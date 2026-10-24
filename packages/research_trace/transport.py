"""Bounded data-only trace channel, separate from the economic result envelope.

No archives, path names, pickle or candidate-provided output destinations cross
this channel. Native stdout is untrusted; portable frames come from the isolated
reviewed engine. Both undergo the same structural and budget validation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from itertools import chain
from pathlib import Path

from pydantic import Field, model_validator

from .contracts import (
    CaptureConfig,
    Contract,
    CoverageState,
    DecisionRecord,
    EvidenceSource,
    canonical_bytes,
)

WIRE_VERSION = "tradegrad.research-wire.v1"
TRACE_PREFIX = "TRADEGRAD_TRACE_V1:"
CAPTURE_FILENAME = "research-capture.jsonl.gz"
MAX_COMPRESSED_BYTES = 48 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 48 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
NATIVE_TRUST = (
    "Native strategy and engine share a process. Emitted observations are not an "
    "independently trusted ledger or no-lookahead proof."
)
PORTABLE_TRUST = (
    "The isolated reviewed engine observes account state and executed orders. "
    "Policy-emitted scores, targets and state are policy claims, not independently "
    "observed internal predicates or a no-lookahead proof."
)


class CaptureSummary(Contract):
    capture: CaptureConfig
    coverage: CoverageState
    missing_reasons: list[str] = Field(max_length=100)
    decisions_seen: int = Field(ge=0, le=2**63 - 1)
    decisions_saved: int = Field(ge=0, le=1_000_000)
    detail_rows_seen: int = Field(ge=0, le=2**63 - 1)
    detail_rows_saved: int = Field(ge=0, le=20_000_000)
    stage_coverage: dict[str, CoverageState] = Field(max_length=100)
    source: EvidenceSource
    trust_boundary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def counts(self):
        if self.capture.mode == "off" and (
            self.decisions_saved
            or self.detail_rows_saved
            or self.coverage != "not_collected"
        ):
            raise ValueError("Disabled capture cannot contain saved observations")
        if (
            self.decisions_saved > self.decisions_seen
            or self.detail_rows_saved > self.detail_rows_seen
        ):
            raise ValueError("Saved capture counts exceed observed counts")
        if (
            self.decisions_saved > self.capture.max_decisions
            or self.detail_rows_saved > self.capture.max_detail_rows
        ):
            raise ValueError("Capture counts exceed configured budgets")
        if any(len(reason) > 500 or not reason for reason in self.missing_reasons):
            raise ValueError("Invalid capture missing reason")
        if any(not 1 <= len(stage) <= 200 for stage in self.stage_coverage):
            raise ValueError("Invalid capture stage identity")
        if self.coverage != "complete" and not self.missing_reasons:
            raise ValueError("Incomplete capture needs a reason")
        if self.coverage == "complete" and (
            self.missing_reasons
            or self.decisions_saved != self.decisions_seen
            or self.detail_rows_saved != self.detail_rows_seen
            or any(value != "complete" for value in self.stage_coverage.values())
        ):
            raise ValueError("Partial capture cannot claim complete coverage")
        return self


def strict_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("Nonfinite trace JSON")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (RecursionError, OverflowError) as error:
        raise ValueError("Invalid trace JSON nesting or number") from error


def _counts(record):
    return sum(
        len(stage.features) + len(stage.predicates) + len(stage.candidates)
        for stage in record.stages
    )


def _validate_scope(record, config):
    from zoneinfo import ZoneInfo

    if config.mode == "off":
        raise ValueError("Disabled capture cannot contain observations")
    window = config.observation_window
    day = record.decision_time.astimezone(ZoneInfo(record.market_timezone)).date()
    if window is not None and not window.start <= day < window.end:
        raise ValueError("Saved decision lies outside capture observation window")
    instruments = set(config.instrument_ids)
    for stage in record.stages:
        if config.stage_ids and stage.stage_id not in config.stage_ids:
            raise ValueError("Saved stage lies outside capture stage scope")
        if config.mode == "summary" and (stage.features or stage.predicates):
            raise ValueError(
                "Summary capture cannot contain detailed feature or predicate rows"
            )
        for row in chain(stage.features, stage.predicates, stage.candidates):
            if (
                instruments
                and row.instrument_id is not None
                and row.instrument_id not in instruments
            ):
                raise ValueError("Saved detail lies outside capture instrument scope")


class CaptureWriter:
    """One gzip stream, incremental bounded chunks and an exact terminal summary.

    A trial compressor prevents a size limit from emitting half of a record.
    Recorder treats a rejected record as partial capture without changing trades.
    """

    def __init__(self, config, emit, *, compressed_limit=MAX_COMPRESSED_BYTES):
        self.config = CaptureConfig.model_validate(config)
        self.emit = emit
        self.compressed_limit = min(compressed_limit, MAX_COMPRESSED_BYTES)
        self.compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.hash = hashlib.sha256()
        self.size = self.raw_size = self.records = self.detail_rows = self.sequence = 0
        self.closed = False
        self.pending = bytearray()
        self._frame("research_start", capture=self.config.model_dump(mode="json"))

    def _frame(self, kind, **fields):
        value = {
            "protocol_version": WIRE_VERSION,
            "kind": kind,
            "sequence": self.sequence,
            **fields,
        }
        self.emit(value)
        self.sequence += 1

    def _bytes(self, data, *, final=False):
        self.hash.update(data)
        self.size += len(data)
        self.pending.extend(data)
        while len(self.pending) >= CHUNK_BYTES or (final and self.pending):
            part = bytes(self.pending[:CHUNK_BYTES])
            del self.pending[:CHUNK_BYTES]
            self._frame("research_chunk", data=base64.b64encode(part).decode("ascii"))

    def append(self, record):
        if self.closed:
            raise ValueError("Capture writer is closed")
        _validate_scope(record, self.config)
        data = canonical_bytes(record) + b"\n"
        if (
            len(data) > MAX_RECORD_BYTES
            or self.raw_size + len(data) > self.config.max_uncompressed_bytes
        ):
            raise ValueError(
                "Capture transport reached its record or uncompressed capacity"
            )
        trial = self.compressor.copy()
        compressed = trial.compress(data) + trial.flush(zlib.Z_SYNC_FLUSH)
        if self.size + len(compressed) + 32 > self.compressed_limit:
            raise ValueError("Capture transport reached its compressed capacity")
        self._bytes(compressed)
        self.compressor = trial
        self.raw_size += len(data)
        self.records += 1
        self.detail_rows += _counts(record)

    def finish(self, summary):
        if self.closed:
            raise ValueError("Duplicate capture finish")
        summary = CaptureSummary.model_validate(summary)
        if summary.capture != self.config or (
            summary.decisions_saved,
            summary.detail_rows_saved,
        ) != (self.records, self.detail_rows):
            raise ValueError("Capture writer summary disagrees with saved records")
        self._bytes(self.compressor.flush(zlib.Z_FINISH), final=True)
        self.closed = True
        self._frame(
            "research_end",
            summary=summary.model_dump(mode="json"),
            sha256=self.hash.hexdigest(),
            size_bytes=self.size,
            uncompressed_bytes=self.raw_size,
        )


class _RecordDecoder:
    def __init__(self, config):
        self.config = config
        self.decompressor = zlib.decompressobj(31)
        self.pending = bytearray()
        self.raw_size = self.records = self.detail_rows = 0
        self.event_keys = set()
        self.decision_ids = set()

    def feed(self, data):
        if self.decompressor.eof and data:
            raise ValueError("Trailing gzip member or data")
        while data:
            try:
                chunk = self.decompressor.decompress(data, 65536)
            except zlib.error as error:
                raise ValueError("Invalid trace gzip") from error
            data = self.decompressor.unconsumed_tail
            self.raw_size += len(chunk)
            if self.raw_size > self.config.max_uncompressed_bytes:
                raise ValueError("Decompressed trace exceeds capture budget")
            self.pending.extend(chunk)
            if self.decompressor.unused_data:
                raise ValueError("Trailing gzip member or data")
            while b"\n" in self.pending:
                index = self.pending.index(b"\n")
                if index + 1 > MAX_RECORD_BYTES:
                    raise ValueError("Trace record exceeds line budget")
                line = bytes(self.pending[:index])
                del self.pending[: index + 1]
                record = DecisionRecord.model_validate(strict_json(line))
                _validate_scope(record, self.config)
                self.records += 1
                self.detail_rows += _counts(record)
                if (
                    self.records > self.config.max_decisions
                    or self.detail_rows > self.config.max_detail_rows
                ):
                    raise ValueError("Trace observations exceed configured budgets")
                key = record.pairing_key()
                if key in self.event_keys or record.decision_id in self.decision_ids:
                    raise ValueError("Duplicate trace decision or source event")
                self.event_keys.add(key)
                self.decision_ids.add(record.decision_id)
                yield record
            if len(self.pending) > MAX_RECORD_BYTES:
                raise ValueError("Trace record exceeds line budget")

    def finish(self):
        if not self.decompressor.eof or self.pending:
            raise ValueError("Incomplete gzip or JSONL trace")


class CaptureReceiver:
    """Accept frames into one supervisor-owned exclusive regular file."""

    def __init__(
        self, output_dir, config, *, source, compressed_limit=MAX_COMPRESSED_BYTES
    ):
        self.path = Path(output_dir) / CAPTURE_FILENAME
        self.config = CaptureConfig.model_validate(config)
        self.source = source
        self.compressed_limit = min(compressed_limit, MAX_COMPRESSED_BYTES)
        self.decoder = _RecordDecoder(self.config)
        self.hash = hashlib.sha256()
        self.size = self.sequence = 0
        self.stream = None
        self.summary = None
        self.started = False

    def accept(self, value):
        if type(value) is not dict or len(canonical_bytes(value)) > MAX_FRAME_BYTES:
            raise ValueError("Oversized or invalid capture frame")
        if (
            value.get("protocol_version") != WIRE_VERSION
            or type(value.get("sequence")) is not int
            or value["sequence"] != self.sequence
        ):
            raise ValueError("Invalid capture frame version or sequence")
        if self.summary is not None:
            raise ValueError("Capture frame after completion")
        kind = value.get("kind")
        common = {"protocol_version", "kind", "sequence"}
        if kind == "research_start":
            if (
                self.started
                or set(value) != common | {"capture"}
                or CaptureConfig.model_validate(value["capture"]) != self.config
            ):
                raise ValueError("Invalid capture start or immutable config mismatch")
            if (
                any(
                    p.is_symlink()
                    for p in (self.path.parent, *self.path.parent.parents)
                )
                or not self.path.parent.is_dir()
            ):
                raise ValueError("Capture output cannot traverse symlinks")
            fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            self.stream = os.fdopen(fd, "wb")
            self.started = True
        elif kind == "research_chunk":
            if (
                not self.started
                or set(value) != common | {"data"}
                or type(value["data"]) is not str
                or len(value["data"]) > 4 * ((CHUNK_BYTES + 2) // 3)
            ):
                raise ValueError("Invalid capture chunk")
            data = base64.b64decode(value["data"], validate=True)
            if (
                not data
                or len(data) > CHUNK_BYTES
                or self.size + len(data) > self.compressed_limit
            ):
                raise ValueError("Capture compressed budget exceeded")
            for _ in self.decoder.feed(data):
                pass
            self.stream.write(data)
            self.hash.update(data)
            self.size += len(data)
        elif kind == "research_end":
            if not self.started or set(value) != common | {
                "summary",
                "sha256",
                "size_bytes",
                "uncompressed_bytes",
            }:
                raise ValueError("Invalid capture terminator")
            self.decoder.finish()
            summary = CaptureSummary.model_validate(value["summary"])
            expected_trust = (
                NATIVE_TRUST if self.source == "strategy_emitted" else PORTABLE_TRUST
            )
            if (
                summary.capture != self.config
                or summary.source != self.source
                or summary.trust_boundary != expected_trust
            ):
                raise ValueError("Capture source, trust or configuration mismatch")
            actual = (
                self.hash.hexdigest(),
                self.size,
                self.decoder.raw_size,
                self.decoder.records,
                self.decoder.detail_rows,
            )
            declared = (
                value["sha256"],
                value["size_bytes"],
                value["uncompressed_bytes"],
                summary.decisions_saved,
                summary.detail_rows_saved,
            )
            if (
                actual != declared
                or type(value["size_bytes"]) is not int
                or type(value["uncompressed_bytes"]) is not int
            ):
                raise ValueError("Capture hash, byte or observation count mismatch")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.summary = summary.model_dump(mode="json")
        else:
            raise ValueError("Unknown capture frame kind")
        self.sequence += 1

    def result(self):
        if self.summary is None:
            raise ValueError("Missing completed research capture")
        return {
            "research_capture": self.summary,
            "research_capture_path": str(self.path),
            "research_capture_sha256": self.hash.hexdigest(),
            "research_capture_size_bytes": self.size,
        }

    def abort(self):
        if self.stream is not None and not self.stream.closed:
            self.stream.close()
        if self.started:
            self.path.unlink(missing_ok=True)


def iter_capture_records(path, expected_sha256, expected_size_bytes, capture_config):
    """Validate hashes and budgets again while a trusted worker consumes JSONL."""
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Capture artifact must be a regular non-symlink file")
    if (
        type(expected_size_bytes) is not int
        or not 0 < expected_size_bytes <= MAX_COMPRESSED_BYTES
        or path.stat().st_size != expected_size_bytes
    ):
        raise ValueError("Capture artifact size mismatch")
    decoder = _RecordDecoder(CaptureConfig.model_validate(capture_config))
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while data := stream.read(CHUNK_BYTES):
            total += len(data)
            if total > expected_size_bytes:
                raise ValueError("Capture artifact grew during read")
            digest.update(data)
            yield from decoder.feed(data)
    decoder.finish()
    if digest.hexdigest() != expected_sha256 or total != expected_size_bytes:
        raise ValueError("Capture artifact checksum mismatch")


class NativeCaptureDemultiplexer:
    """Incremental stdout splitting; never retain the potentially large trace."""

    def __init__(self, receiver, *, other_limit):
        self.receiver = receiver
        self.other_limit = other_limit
        self.pending = bytearray()
        self.other = bytearray()

    def feed(self, data):
        self.pending.extend(data)
        while b"\n" in self.pending:
            index = self.pending.index(b"\n")
            line = bytes(self.pending[:index])
            del self.pending[: index + 1]
            self._line(line)
        # Only economic result lines may be large. Ordinary logs are bounded too.
        if len(self.pending) > max(self.other_limit, MAX_FRAME_BYTES):
            raise ValueError("Native stdout line exceeds its budget")

    def _line(self, line):
        prefix = TRACE_PREFIX.encode()
        if line.startswith(prefix):
            if len(line) > MAX_FRAME_BYTES + len(prefix):
                raise ValueError("Native capture frame exceeds its budget")
            self.receiver.accept(strict_json(line[len(prefix) :]))
        else:
            if len(self.other) + len(line) + 1 > self.other_limit:
                raise ValueError("Native ordinary/result stdout exceeds its budget")
            self.other.extend(line + b"\n")

    def finish(self):
        if self.pending:
            self._line(bytes(self.pending))
            self.pending.clear()
        return bytes(self.other)
