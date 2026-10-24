"""Trusted fixed-SQL comparison I/O with bounded, cancellable owned DuckDB.

This internal batch reader is intentionally separate from the browser TraceReader.
It never accepts SQL, extensions, candidate-written Parquet, or browser paths.
The business owner holds its attempt-directory lock and lease until this module
has synchronously closed every connection and joined every interrupt watchdog.
"""

from __future__ import annotations

import hashlib
import json
import math
import resource
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import duckdb

from .comparison_codec import (
    PROJECTED_FIELDS,
    decode_record,
    encode_record,
    expanded_size_sql,
    unsafe_numeric_sql,
)
from .comparison_contracts import (
    DEFAULT_LIMITS,
    FIELD_TOLERANCES,
    READABLE_ALGORITHM_VERSIONS,
    SECTIONS,
    TOLERANCE_VERSION,
    ComparisonArtifactRef,
    ComparisonError,
    ComparisonPage,
    ComparisonRecord,
    WrittenComparisonArtifact,
)
from .comparison_semantics import (
    CanonicalObservation,
    observation_payload_size,
    observed_value,
)
from .contracts import canonical_bytes, digest
from .storage import SCHEMAS, TraceReader, TraceStorageError, _table_ref, _verified_file

MAX_PAGE = 500
MAX_OFFSET = 100_000
MAX_RESPONSE_BYTES = 4 * 1024**2
MAX_READ_BYTES = 128 * 1024**2
_OUTPUT_SCHEMA = {
    "row_number": "BIGINT",
    "event_pair_id": "VARCHAR",
    "event_pair_ids": "VARCHAR",
    "session_date": "VARCHAR",
    "instrument_id": "VARCHAR",
    "stage_id": "VARCHAR",
    "status": "VARCHAR",
    "payload": "VARCHAR",
    "expanded_payload_bytes": "BIGINT",
}


def _connect():
    return duckdb.connect(
        config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "allow_unsigned_extensions": "false",
            "threads": "1",
            "memory_limit": "256MiB",
            "max_temp_directory_size": "0B",
        }
    )


class ComparisonControl:
    """Cancellation is cooperative in Python and repeatedly interrupts DuckDB.

    No detached query thread exists. The caller executes queries synchronously;
    the only helper thread repeatedly interrupts its owned live connection.
    Connection close finishes before its watchdog is stopped and joined.
    """

    def __init__(self, *, cancel_event=None, timeout_seconds=300, deadline=None):
        if (
            type(timeout_seconds) not in (float, int)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("A finite positive comparison timeout is required")
        if deadline is not None and (
            type(deadline) not in (float, int) or not math.isfinite(deadline)
        ):
            raise ValueError("A finite monotonic deadline is required")
        self.cancel_event = (
            cancel_event if cancel_event is not None else threading.Event()
        )
        self.deadline = min(
            time.monotonic() + timeout_seconds,
            deadline if deadline is not None else math.inf,
        )
        self._dispose_event = threading.Event()
        self._lock = threading.Lock()
        self._active = set()
        self._unconfirmed = set()

    def cancel(self):
        self.cancel_event.set()

    def check(self):
        if self.cancel_event.is_set() or self._dispose_event.is_set():
            raise ComparisonError("comparison_cancelled", "Comparison was cancelled")
        if time.monotonic() >= self.deadline:
            raise ComparisonError(
                "comparison_timeout", "Comparison exceeded its fixed runtime budget"
            )

    @property
    def active_connections(self):
        with self._lock:
            return len(self._active)

    @property
    def closure_unconfirmed(self):
        with self._lock:
            return bool(self._unconfirmed)

    @property
    def closure_confirmed(self):
        with self._lock:
            return not self._active and not self._unconfirmed

    @contextmanager
    def connection(self):
        self.check()
        connection = _connect()
        closed = threading.Event()
        with self._lock:
            self._active.add(closed)

        def interrupt_until_closed():
            while not closed.wait(0.025):
                if (
                    self.cancel_event.is_set()
                    or self._dispose_event.is_set()
                    or time.monotonic() >= self.deadline
                ):
                    try:
                        connection.interrupt()
                    except Exception:  # noqa: BLE001, S110 - an interrupt races synchronous close
                        # A concurrent synchronous close may finish between the
                        # closed flag check and interrupt. Never stop early.
                        pass

        watcher = threading.Thread(
            target=interrupt_until_closed, name="comparison-interrupt", daemon=False
        )
        started = False
        try:
            watcher.start()
            started = True
            self.check()
            yield connection
            self.check()
        except duckdb.Error:
            self.check()
            raise ComparisonError() from None
        finally:
            # A close exception is not proof of termination. Retain the caller's
            # lock/lease and the live watcher until a synchronous close succeeds.
            # Permanent failure requires process termination/recovery, not a
            # timeout-based stopped marker. The bounded backoff is not a query
            # thread and does not release any ownership guard.
            while True:
                try:
                    connection.close()
                    break
                except BaseException:  # noqa: BLE001 - unknown closure must retain ownership
                    try:
                        with self._lock:
                            self._unconfirmed.add(closed)
                        self._dispose_event.set()
                        time.sleep(0.25)
                    except BaseException:  # noqa: BLE001, S112 - shutdown during backoff is not closure proof
                        # Repeated shutdown signals must not escape this owned
                        # cleanup path while synchronous closure is unconfirmed.
                        continue
            closed.set()
            if started:
                watcher.join()
            with self._lock:
                self._active.discard(closed)
                self._unconfirmed.discard(closed)

    def close(self):
        """Await disposal without mutating the caller's cancellation event.

        An idle normal close is a no-op. Active disposal uses a private signal,
        so successful worker teardown cannot become a user cancellation.
        """
        while True:
            with self._lock:
                pending = tuple(self._active)
            if not pending:
                return
            self._dispose_event.set()
            for closed in pending:
                closed.wait(0.025)


def manifest_name(comparison_id):
    return f"comparison-{comparison_id}-manifest.json"


def check_file_budget(count, limits):
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    available = (
        limits.max_files if soft == resource.RLIM_INFINITY else max(0, soft - 64)
    )
    if count > min(limits.max_files, available):
        raise ComparisonError(
            "comparison_capacity", "Comparison exceeds its fixed open-file budget"
        )


_ROW_SIZE_ENCODER = json.JSONEncoder(separators=(",", ":"), allow_nan=False)


def _json_row_size(row):
    # The SQL scalar columns and decoded JSON payload have only string keys.
    # Sorting those keys cannot change the encoded length. ensure_ascii=True
    # also makes character and UTF-8 byte counts identical, avoiding a second
    # byte allocation. This is size accounting only, never persisted/hash bytes.
    return len(_ROW_SIZE_ENCODER.encode(row))


class TrustedTraceScan:
    """One operation's hash/inode-pinned immutable trace files.

    Input references are admitted by the caller. Each verified inode remains open
    through every DuckDB read, so there is no hash-then-reopen pathname race.
    """

    def __init__(self, value, stack, control, limits=DEFAULT_LIMITS):
        self.manifest = value.trace_manifest
        self.control, self.limits = control, limits
        self.files = {}
        if self.manifest is None:
            return
        # Reuse schema/name validation only, never its browser query/pagination.
        TraceReader(self.manifest, value.trace_paths)
        check_file_budget(len(self.manifest.artifacts), limits)
        try:
            for ref in self.manifest.artifacts:
                control.check()
                pinned = stack.enter_context(
                    _verified_file(value.trace_paths[ref.name], ref)
                )
                self.files[ref.name] = pinned
                control.check()
        except (TraceStorageError, KeyError, OSError):
            raise ComparisonError() from None

    def refs(self, table, day=None):
        if table not in SCHEMAS:
            raise ComparisonError()
        if self.manifest is None:
            return []
        return [
            ref
            for ref in self.manifest.artifacts
            if _table_ref(self.manifest, ref, table)
            and (
                day is None
                or ref.date_range.start.isoformat()
                <= day
                < ref.date_range.end.isoformat()
            )
        ]

    def source(self, table, day=None):
        refs = self.refs(table, day)
        return [self.files[ref.name] for ref in refs]

    def rows(
        self,
        connection,
        table,
        *,
        decision_id=None,
        day=None,
        semantic_projection=False,
    ):
        self.control.check()
        paths = self.source(table, day)
        if not paths:
            return
        if table != "events" and decision_id is None:
            raise ComparisonError()
        projection = ",".join(
            f"CAST(\"{key}\" AT TIME ZONE 'UTC' AS VARCHAR) || '+00:00' AS \"{key}\""
            if kind == "TIMESTAMPTZ"
            else f'"{key}"'
            for key, kind in SCHEMAS[table].items()
        )
        where, args = (
            ("decision_id = ?", [paths, decision_id])
            if decision_id is not None
            else ("true", [paths])
        )
        order = (
            "decision_time,event_seq,decision_id"
            if table == "events"
            else (
                "stage_id,ordinal"
                if table in {"candidates", "features", "predicates"}
                else "decision_id"
                if table == "decisions"
                else "stage_id"
            )
        )
        try:
            if table == "events":
                # Preflight before even the first fetchmany: event metadata can
                # contain long source keys and many long stage IDs. Six times
                # UTF-8 text bytes bounds JSON escaping, plus fixed scalar/key
                # overhead. Conservative rejection is preferable to materializing
                # an oversized index and noticing only afterwards.
                text_size = " + ".join(
                    f'coalesce(octet_length(encode("{key}")),0)'
                    for key, kind in SCHEMAS[table].items()
                    if kind == "VARCHAR"
                )
                estimate = f"(512 + 6 * ({text_size}))"
                count, total_bytes, largest = connection.execute(
                    f"SELECT count(*), coalesce(sum({estimate}),0), coalesce(max({estimate}),0) FROM read_parquet(?) WHERE {where}",
                    args,
                ).fetchone()
                if (
                    count > self.limits.max_events
                    or total_bytes > self.limits.max_event_index_bytes
                    or largest > self.limits.max_record_bytes
                ):
                    raise ComparisonError(
                        "comparison_capacity",
                        "The event index exceeds its fixed pre-materialization row/byte budget",
                    )
            if "payload" in SCHEMAS[table]:
                count, total_bytes, largest = connection.execute(
                    f"SELECT count(*), coalesce(sum(octet_length(encode(payload))),0), coalesce(max(octet_length(encode(payload))),0) FROM read_parquet(?) WHERE {where}",
                    args,
                ).fetchone()
                if (
                    count > self.limits.max_event_rows
                    or total_bytes > self.limits.max_event_bytes
                    or largest > self.limits.max_record_bytes
                ):
                    raise ComparisonError(
                        "comparison_capacity",
                        "An exact input section exceeds its fixed pre-materialization row/byte budget",
                    )
            cursor = connection.execute(
                f"SELECT {projection} FROM read_parquet(?) WHERE {where} ORDER BY {order}",
                args,
            )
            keys = [entry[0] for entry in cursor.description]
            count = size = 0
            while batch := cursor.fetchmany(64):
                self.control.check()
                for raw in batch:
                    row = dict(zip(keys, raw, strict=True))
                    count += 1
                    maximum = (
                        self.limits.max_events
                        if table == "events"
                        else self.limits.max_event_rows
                    )
                    budget = (
                        self.limits.max_event_index_bytes
                        if table == "events"
                        else self.limits.max_event_bytes
                    )
                    if "payload" in row:
                        if len(row["payload"].encode()) > self.limits.max_record_bytes:
                            raise ComparisonError(
                                "comparison_capacity",
                                "An exact input observation exceeds the comparison record budget",
                            )
                        row["payload"] = json.loads(row["payload"])
                    if "stage_ids" in row:
                        row["stage_ids"] = json.loads(row["stage_ids"])
                    if "session_date" in row:
                        row["session_date"] = str(row["session_date"])
                    if semantic_projection:
                        # Serialize the semantic projection only once and retain
                        # it instead of a copied semantic dictionary. Its exact
                        # bytes are reused by bounded matching. Reconstruct the
                        # full source-row size additively, without omitting any
                        # provenance or fixed-column bytes from admission.
                        payload = row["payload"]
                        semantic = observed_value(payload)
                        encoded_semantic = CanonicalObservation(
                            canonical_bytes(semantic), payload
                        )
                        payload_size = observation_payload_size(
                            payload, len(encoded_semantic.raw)
                        )
                        row["payload"] = None
                        row_size = _json_row_size(row) - 4 + payload_size
                        row["payload"] = payload
                        # The previous representation is a shallow dictionary
                        # sharing nested objects with the retained source record.
                        # Only replace it when the complete byte object consumes
                        # no more memory than that dictionary alone. Large/nested
                        # observations keep the original shared representation;
                        # no extra uncharged source payload is retained.
                        if sys.getsizeof(encoded_semantic) + sys.getsizeof(
                            encoded_semantic.raw
                        ) <= sys.getsizeof(semantic):
                            semantic = encoded_semantic
                        del encoded_semantic
                    else:
                        row_size = _json_row_size(row)
                    size += row_size + 1
                    if count > maximum or size + 2 > budget:
                        raise ComparisonError(
                            "comparison_capacity",
                            "An exact comparison section exceeds its fixed row/byte budget",
                        )
                    if semantic_projection:
                        row["_semantic"] = semantic
                    yield row
        except duckdb.Error:
            self.control.check()
            raise ComparisonError() from None


class ComparisonWriter:
    """Bounded date/event-indexed row-group blocks, with manifest published last."""

    def __init__(
        self,
        target,
        publication,
        control,
        connection,
        *,
        limits=DEFAULT_LIMITS,
        durable_root=None,
    ):
        self.target, self.publication = Path(target), publication
        self.control, self.connection, self.limits = control, connection, limits
        self.durable_root = durable_root
        self.buffers = {section: [] for section in SECTIONS}
        self.sizes = dict.fromkeys(SECTIONS, 0)
        self.counts = dict.fromkeys(SECTIONS, 0)
        self.sequence = dict.fromkeys(SECTIONS, 0)
        self.artifacts, self.refs = [], []
        self.total_bytes = self.total_rows = 0
        self.total_expanded_bytes = 0
        self.published_bytes = 0
        self._last_pair_id = None
        self._last_pair_ids_json = "[]"

    def append(self, record):
        self.control.check()
        record = ComparisonRecord.model_validate(record)
        raw, expanded_bytes = encode_record(record)
        if expanded_bytes > self.limits.max_record_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "An exact comparison record exceeds the output record budget",
            )
        section = record.section
        if self.buffers[section] and (
            len(self.buffers[section]) >= self.limits.block_rows
            or self.sizes[section] + expanded_bytes > self.limits.block_bytes
        ):
            self.flush(section)
        self.total_rows += 1
        # As before, this aggregate counts the payload column, not the other
        # index columns. It now bounds versioned compact JSON payload bytes.
        # Public record/block/response limits continue to bound expanded bytes;
        # published artifacts have their own unchanged aggregate cap below.
        self.total_bytes += len(raw)
        self.total_expanded_bytes += expanded_bytes
        if (
            self.total_rows > self.limits.max_output_rows
            or self.total_bytes > self.limits.max_output_bytes
        ):
            raise ComparisonError(
                "comparison_capacity",
                "Comparison exceeds its fixed output row/byte budget; nothing was truncated",
            )
        index = self.counts[section]
        self.counts[section] += 1
        if record.associations:
            pair_ids_json = canonical_bytes(
                sorted(
                    {item.event_pair_id for item in record.associations}
                    | ({record.event_pair_id} if record.event_pair_id else set())
                )
            ).decode()
        else:
            # Only the immediately preceding scalar is retained, never evidence
            # or an unbounded cache. Most rows in an event share this exact index.
            if record.event_pair_id != self._last_pair_id:
                self._last_pair_id = record.event_pair_id
                self._last_pair_ids_json = canonical_bytes(
                    [record.event_pair_id] if record.event_pair_id else []
                ).decode()
            pair_ids_json = self._last_pair_ids_json
        self.buffers[section].append(
            {
                "row_number": index,
                "event_pair_id": record.event_pair_id,
                "event_pair_ids": pair_ids_json,
                "session_date": record.session_date,
                "instrument_id": record.instrument_id,
                "stage_id": record.stage_id,
                "status": record.status,
                "payload": raw.decode(),
                "expanded_payload_bytes": expanded_bytes,
            }
        )
        self.sizes[section] += expanded_bytes

    def _publish(self, name, raw, media_type, rows, section):
        from backend.app.core.atomic_storage import publish_bytes

        self.control.check()
        if len(self.artifacts) >= self.limits.max_files:
            raise ComparisonError(
                "comparison_capacity", "Comparison exceeds its fixed output file budget"
            )
        if self.published_bytes + len(raw) > self.limits.max_output_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison exceeds its aggregate published-artifact byte budget",
            )
        sha = hashlib.sha256(raw).hexdigest()
        path = self.target / name
        publish_bytes(path, raw, sha256=sha, durable_root=self.durable_root)
        self.control.check()
        item = WrittenComparisonArtifact(
            name, path, sha, len(raw), media_type, rows, section
        )
        self.artifacts.append(item)
        self.published_bytes += len(raw)
        return item

    def flush(self, section):
        self.control.check()
        rows = self.buffers[section]
        if not rows:
            return
        # Deduplicate the exact physical index strings before JSON decoding.
        # Equal strings necessarily denote the same index, including duplicates
        # and explicit associations; no observation payload is deduplicated.
        pair_ids = sorted(
            {
                pair_id
                for encoded in {row["event_pair_ids"] for row in rows}
                for pair_id in json.loads(encoded)
            }
        )
        if len(pair_ids) > 16384:
            raise ComparisonError(
                "comparison_capacity",
                "One exact output block exceeds its fixed event-association index budget",
            )
        with tempfile.TemporaryDirectory(prefix="comparison-block-") as temporary:
            source, target = (
                Path(temporary) / "rows.jsonl",
                Path(temporary) / "rows.parquet",
            )
            largest = 0
            with source.open("wb") as stream:
                for row in rows:
                    self.control.check()
                    # This is only the transient JSON-to-Parquet envelope.
                    # Fixed scalar columns need no sorted object keys; payload
                    # remains the original exact canonical JSON string.
                    encoded = _ROW_SIZE_ENCODER.encode(row).encode()
                    largest = max(largest, len(encoded))
                    stream.write(encoded + b"\n")
            object_size = max(4 * 1024**2, largest + 1024)
            if object_size > 64 * 1024**2:
                raise ComparisonError(
                    "comparison_capacity",
                    "Encoded comparison record exceeds the trusted JSON-to-Parquet buffer budget",
                )
            columns = canonical_bytes(_OUTPUT_SCHEMA).decode()
            self.connection.execute(
                f"COPY (SELECT * FROM read_json($input, format='newline_delimited', columns={columns}, maximum_object_size={object_size}) ORDER BY row_number) TO $output (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)",
                {"input": str(source), "output": str(target)},
            )
            self.control.check()
            if target.stat().st_size > self.limits.max_output_bytes:
                raise ComparisonError(
                    "comparison_capacity",
                    "Comparison output block exceeds the byte budget",
                )
            self.sequence[section] += 1
            name = f"comparison-{self.publication.comparison_id}-{section}-{self.sequence[section]:06d}.parquet"
            item = self._publish(
                name,
                target.read_bytes(),
                "application/vnd.apache.parquet",
                len(rows),
                section,
            )
        self.refs.append(
            ComparisonArtifactRef(
                name=item.name,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                rows=item.rows,
                section=section,
                event_pair_ids=pair_ids,
                first_row=rows[0]["row_number"],
                last_row=rows[-1]["row_number"],
            )
        )
        self.buffers[section] = []
        self.sizes[section] = 0

    def finish_blocks(self):
        for section in SECTIONS:
            self.flush(section)

    def publish_manifest(self, manifest):
        raw = canonical_bytes(manifest)
        if len(raw) > self.limits.max_manifest_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison manifest exceeds its fixed byte budget",
            )
        self._publish(
            manifest_name(self.publication.comparison_id),
            raw,
            "application/json",
            1,
            None,
        )
        return manifest, self.artifacts


class ComparisonReader:
    """Exact hash-verified bounded projections of immutable comparison blocks."""

    def __init__(self, manifest, paths):
        self.manifest, self.paths = manifest, paths
        # Validate the persisted declaration, not the current computation
        # default. Historical v1 bytes remain readable at their original hash.
        if (
            manifest.algorithm_version not in READABLE_ALGORITHM_VERSIONS
            or manifest.tolerance_version != TOLERANCE_VERSION
            or manifest.field_tolerances != FIELD_TOLERANCES
            or manifest.input_hash
            != digest(
                {
                    "left": manifest.left.model_dump(mode="json"),
                    "right": manifest.right.model_dump(mode="json"),
                    "algorithm": manifest.algorithm_version,
                    "tolerance_version": manifest.tolerance_version,
                    "tolerances": manifest.field_tolerances,
                    "limits": manifest.limits.model_dump(mode="json"),
                }
            )
        ):
            raise ComparisonError()
        for ref in manifest.artifacts:
            prefix = f"comparison-{manifest.publication.comparison_id}-{ref.section}-"
            if (
                ref.section not in SECTIONS
                or not ref.name.startswith(prefix)
                or not ref.name.endswith(".parquet")
                or ref.last_row < ref.first_row
                or ref.rows != ref.last_row - ref.first_row + 1
            ):
                raise ComparisonError()

    def page(
        self,
        section,
        *,
        event_pair_id=None,
        instrument_id=None,
        offset=0,
        limit=100,
        control=None,
    ):
        if (
            section not in SECTIONS
            or type(offset) is not int
            or not 0 <= offset <= MAX_OFFSET
            or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE
            or any(
                value is not None
                and (not isinstance(value, str) or not value or len(value) > 200)
                for value in (event_pair_id, instrument_id)
            )
        ):
            raise ComparisonError(
                "invalid_comparison_query",
                "Invalid comparison section, filter or pagination bounds",
            )
        refs = [
            ref
            for ref in self.manifest.artifacts
            if ref.section == section
            and (event_pair_id is None or event_pair_id in ref.event_pair_ids)
        ]
        if sum(ref.size_bytes for ref in refs) > MAX_READ_BYTES:
            raise ComparisonError(
                "comparison_query_too_large",
                "Select an exact event; this comparison section exceeds the browser scan budget",
            )
        check_file_budget(len(refs), self.manifest.limits)
        if not refs:
            return ComparisonPage(
                section=section,
                availability=self.manifest.sections[section],
                items=[],
                total=0,
                offset=offset,
                limit=limit,
            )
        control = control or ComparisonControl(timeout_seconds=30)
        conditions, args = [], []
        for field, value in (
            ("event_pair_id", event_pair_id),
            ("instrument_id", instrument_id),
        ):
            if value is not None:
                conditions.append(
                    "list_contains(CAST(event_pair_ids AS VARCHAR[]), ?)"
                    if field == "event_pair_id"
                    else "instrument_id = ?"
                )
                args.append(value)
        where = " AND ".join(conditions) or "true"
        try:
            with ExitStack() as stack:
                paths = []
                for ref in refs:
                    control.check()
                    paths.append(
                        stack.enter_context(_verified_file(self.paths[ref.name], ref))
                    )
                with control.connection() as connection:
                    # Historical blocks have no decoded-size column. A union
                    # permits mixed old/new blocks without rewriting either
                    # artifact or changing its pinned checksum.
                    schema = {
                        row[0]: row[1]
                        for row in connection.execute(
                            "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)",
                            [paths],
                        ).fetchall()
                    }
                    if (
                        not (_OUTPUT_SCHEMA.keys() - {"expanded_payload_bytes"})
                        <= schema.keys()
                    ):
                        raise ComparisonError()
                    has_expanded = "expanded_payload_bytes" in schema
                    if has_expanded and schema["expanded_payload_bytes"] != "BIGINT":
                        raise ComparisonError()
                    declared_size = "expanded_payload_bytes" if has_expanded else "NULL"
                    expanded_size = expanded_size_sql(section)
                    # Legacy payloads contain their projections too. Bound the
                    # separately fetched columns independently, including for
                    # a corrupt legacy row whose index disagrees with payload.
                    projected_size = " + ".join(
                        f"coalesce(octet_length(encode({field})),0)"
                        for field in PROJECTED_FIELDS
                        if field != "section"
                    )
                    total = connection.execute(
                        f"SELECT count(*) FROM read_parquet(?, union_by_name=true) WHERE {where}",
                        [paths, *args],
                    ).fetchone()[0]
                    size, largest, invalid, projected_bytes = connection.execute(
                        f"SELECT coalesce(sum(actual_size),0),coalesce(max(actual_size),0),count(*) FILTER (WHERE (declared_size IS NOT NULL AND declared_size != actual_size) OR actual_size <= 0 OR regexp_matches(payload, '[^\\x00-\\x7E]') OR {unsafe_numeric_sql()}),coalesce(sum(projected_size),0) FROM (SELECT payload,{expanded_size} AS actual_size,{declared_size} AS declared_size,{projected_size} AS projected_size FROM read_parquet(?, union_by_name=true) WHERE {where} ORDER BY row_number LIMIT ? OFFSET ?)",
                        [paths, *args, limit, offset],
                    ).fetchone()
                    if invalid or largest > self.manifest.limits.max_record_bytes:
                        raise ComparisonError()
                    if largest > MAX_RESPONSE_BYTES:
                        raise ComparisonError(
                            "comparison_detail_too_large",
                            "An exact comparison observation exceeds the browser detail budget; its immutable artifact retains all bytes",
                        )
                    if (
                        size > MAX_RESPONSE_BYTES
                        or projected_bytes > MAX_RESPONSE_BYTES
                    ):
                        raise ComparisonError(
                            "comparison_query_too_large",
                            "Narrow the comparison selection or page size",
                        )
                    rows = connection.execute(
                        f"SELECT payload,{declared_size},event_pair_id,session_date,instrument_id,stage_id,status FROM read_parquet(?, union_by_name=true) WHERE {where} ORDER BY row_number LIMIT ? OFFSET ?",
                        [paths, *args, limit, offset],
                    ).fetchall()
                    items, decoded_bytes = [], 0
                    for row in rows:
                        control.check()
                        item, item_size = decode_record(
                            row[0],
                            dict(
                                zip(PROJECTED_FIELDS, (section, *row[2:]), strict=True)
                            ),
                            row[1],
                        )
                        if item_size > self.manifest.limits.max_record_bytes:
                            raise ComparisonError()
                        if item_size > MAX_RESPONSE_BYTES:
                            raise ComparisonError("comparison_detail_too_large")
                        decoded_bytes += item_size
                        if decoded_bytes > MAX_RESPONSE_BYTES:
                            raise ComparisonError("comparison_query_too_large")
                        items.append(item)
            page = ComparisonPage(
                section=section,
                availability=self.manifest.sections[section],
                items=items,
                total=total,
                offset=offset,
                limit=limit,
            )
            if len(canonical_bytes(page)) > MAX_RESPONSE_BYTES:
                raise ComparisonError(
                    "comparison_query_too_large",
                    "The complete comparison response exceeds its fixed byte budget",
                )
            return page
        except ComparisonError:
            raise
        except (
            TraceStorageError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            duckdb.Error,
        ):
            control.check()
            raise ComparisonError() from None
