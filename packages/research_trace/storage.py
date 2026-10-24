"""Immutable, bounded Parquet evidence. Never imports or re-evaluates a strategy.

Only fixed SQL identifiers occur below. Paths are server-owned manifest entries,
not SQL or URLs supplied by a browser. JSON payload columns preserve the versioned
contracts while scalar columns permit predicate and projection pushdown.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from .contracts import (
    ArtifactRef,
    DecisionRecord,
    EvaluationBinding,
    TraceManifest,
    canonical_bytes,
)
from .transport import MAX_RECORD_BYTES

MAX_PAGE = 500
MAX_OFFSET = 100_000
MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
BLOCK_ROWS = 16_384
BLOCK_BYTES = 8 * 1024 * 1024
# Nested JSON payload escaping can nearly double the accepted wire row.
MAX_STORED_ROW_BYTES = 2 * MAX_RECORD_BYTES + 65536
TABLES = ("events", "decisions", "stages", "candidates", "features", "predicates")
_BASE = {"decision_id": "VARCHAR", "session_date": "DATE"}
SCHEMAS = {
    "events": {
        **_BASE,
        "decision_time": "TIMESTAMPTZ",
        "information_cutoff": "TIMESTAMPTZ",
        "event_seq": "BIGINT",
        "data_event_id": "VARCHAR",
        "scope": "VARCHAR",
        "instrument_id": "VARCHAR",
        "callback_phase": "VARCHAR",
        "market_timezone": "VARCHAR",
        "stage_ids": "VARCHAR",
    },
    "decisions": {**_BASE, "payload": "VARCHAR"},
    "stages": {**_BASE, "stage_id": "VARCHAR", "payload": "VARCHAR"},
    "candidates": {
        **_BASE,
        "stage_id": "VARCHAR",
        "ordinal": "BIGINT",
        "instrument_id": "VARCHAR",
        "eligibility": "VARCHAR",
        "rank": "BIGINT",
        "score": "DOUBLE",
        "reason": "VARCHAR",
        "payload": "VARCHAR",
    },
    "features": {
        **_BASE,
        "stage_id": "VARCHAR",
        "ordinal": "BIGINT",
        "instrument_id": "VARCHAR",
        "payload": "VARCHAR",
    },
    "predicates": {
        **_BASE,
        "stage_id": "VARCHAR",
        "ordinal": "BIGINT",
        "instrument_id": "VARCHAR",
        "payload": "VARCHAR",
    },
}


class TraceStorageError(ValueError):
    """Safe categorical errors; underlying file/SQL diagnostics are not exposed."""

    def __init__(
        self,
        code="trace_integrity",
        message="Trace evidence failed integrity verification",
    ):
        self.code, self.message = code, message
        super().__init__(message)


def _connection():
    return duckdb.connect(
        config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "threads": "1",
            "memory_limit": "256MB",
            "max_temp_directory_size": "0B",
        }
    )


def manifest_name(evaluation_id):
    return f"evaluation-{evaluation_id}-trace-manifest.json"


@dataclass(frozen=True)
class WrittenTraceArtifact:
    name: str
    path: Path
    sha256: str
    size_bytes: int
    media_type: str
    rows: int


def _json(value):
    return canonical_bytes(value).decode()


def write_trace_artifacts(
    records: Iterable[DecisionRecord],
    binding: EvaluationBinding,
    capture_summary: Mapping,
    target_path: Path,
    *,
    durable_root: Path | None = None,
):
    """Stream records into bounded date-range blocks, publish the manifest last.

    Returns (TraceManifest, artifacts including the manifest). Exact repeated
    publication is allowed, but different bytes can never replace an existing
    name. A failed publication must not be registered in the database. The caller
    owns managed-storage admission and its storage retention barrier.
    """
    from backend.app.core.atomic_storage import publish_bytes

    target_path = Path(target_path)
    # Validate the complete bounded summary before processing the stream.
    from .transport import CaptureSummary

    try:
        captured = CaptureSummary.model_validate(dict(capture_summary))
        summary = TraceManifest.model_validate(
            {**captured.model_dump(mode="json"), "binding": binding, "artifacts": []}
        )
    except (ValueError, TypeError):
        raise TraceStorageError() from None
    artifacts, refs = [], []
    buffers = {name: [] for name in TABLES}
    sizes = {name: 0 for name in TABLES}
    ranges = {name: [] for name in TABLES}
    sequence = {name: 0 for name in TABLES}
    seen_ids, seen_events = set(), set()
    decisions = details = total_bytes = 0

    def publish(name, raw, media_type, rows):
        sha = hashlib.sha256(raw).hexdigest()
        path = target_path / name
        publish_bytes(path, raw, sha256=sha, durable_root=durable_root)
        artifacts.append(
            WrittenTraceArtifact(name, path, sha, len(raw), media_type, rows)
        )
        return artifacts[-1]

    try:
        with (
            tempfile.TemporaryDirectory(prefix="tradegrad-trace-") as temporary,
            _connection() as connection,
        ):
            temporary = Path(temporary)

            def flush(table):
                if not buffers[table]:
                    return
                source, output = temporary / "rows.jsonl", temporary / "rows.parquet"
                source.write_bytes(b"\n".join(buffers[table]) + b"\n")
                output.unlink(missing_ok=True)
                # SCHEMAS is a fixed internal allowlist. Filenames are bound values.
                columns = _json(SCHEMAS[table])
                ordered = ", ".join(
                    key
                    for key in ("session_date", "decision_id", "stage_id", "ordinal")
                    if key in SCHEMAS[table]
                )
                connection.execute(
                    f"COPY (SELECT * FROM read_json($input, format='newline_delimited', columns={columns}, maximum_object_size=67108864) ORDER BY {ordered}) TO $output (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)",
                    {"input": str(source), "output": str(output)},
                )
                sequence[table] += 1
                name = f"evaluation-{binding.evaluation_id}-trace-{table}-{min(ranges[table]):%Y-%m}-{sequence[table]:06d}.parquet"
                item = publish(
                    name,
                    output.read_bytes(),
                    "application/vnd.apache.parquet",
                    len(buffers[table]),
                )
                refs.append(
                    ArtifactRef(
                        name=name,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        rows=item.rows,
                        media_type=item.media_type,
                        date_range={
                            "start": min(ranges[table]),
                            "end": max(ranges[table]) + timedelta(days=1),
                        },
                    )
                )
                buffers[table].clear()
                ranges[table].clear()
                sizes[table] = 0

            def append(table, row):
                raw = canonical_bytes(row)
                if len(raw) > MAX_STORED_ROW_BYTES:
                    raise TraceStorageError(
                        "trace_record_too_large",
                        "One trace row exceeds the bounded storage record limit",
                    )
                if buffers[table] and (
                    len(buffers[table]) >= BLOCK_ROWS
                    or sizes[table] + len(raw) > BLOCK_BYTES
                ):
                    flush(table)
                buffers[table].append(raw)
                sizes[table] += len(raw)
                ranges[table].append(date.fromisoformat(row["session_date"]))

            for record in records:
                record = DecisionRecord.model_validate(record)
                if (
                    record.decision_id in seen_ids
                    or record.pairing_key() in seen_events
                ):
                    raise TraceStorageError()
                seen_ids.add(record.decision_id)
                seen_events.add(record.pairing_key())
                decisions += 1
                total_bytes += len(canonical_bytes(record))
                if (
                    decisions > summary.capture.max_decisions
                    or total_bytes > summary.capture.max_uncompressed_bytes
                ):
                    raise TraceStorageError(
                        "trace_capacity",
                        "Saved trace exceeds its declared capture capacity",
                    )
                body = record.model_dump(mode="json")
                day = (
                    record.decision_time.astimezone(ZoneInfo(record.market_timezone))
                    .date()
                    .isoformat()
                )
                base = {"decision_id": record.decision_id, "session_date": day}
                append(
                    "events",
                    {
                        **base,
                        **{
                            key: body[key]
                            for key in SCHEMAS["events"]
                            if key not in {*base, "stage_ids"}
                        },
                        "stage_ids": _json([stage.stage_id for stage in record.stages]),
                    },
                )
                append(
                    "decisions",
                    {
                        **base,
                        "payload": _json(
                            {
                                key: value
                                for key, value in body.items()
                                if key != "stages"
                            }
                        ),
                    },
                )
                for stage in record.stages:
                    stage_body = stage.model_dump(mode="json")
                    stage_base = {**base, "stage_id": stage.stage_id}
                    append(
                        "stages",
                        {
                            **stage_base,
                            "payload": _json(
                                {
                                    **{
                                        key: value
                                        for key, value in stage_body.items()
                                        if key
                                        not in {"features", "predicates", "candidates"}
                                    },
                                    **{
                                        f"{kind}_saved": len(stage_body[kind])
                                        for kind in (
                                            "features",
                                            "predicates",
                                            "candidates",
                                        )
                                    },
                                }
                            ),
                        },
                    )
                    for table in ("candidates", "features", "predicates"):
                        for ordinal, item in enumerate(stage_body[table]):
                            if (
                                item.get("source_ref")
                                and item["source_ref"]["code_sha256"]
                                != binding.code_sha256
                            ):
                                raise TraceStorageError()
                            details += 1
                            if details > summary.capture.max_detail_rows:
                                raise TraceStorageError(
                                    "trace_capacity",
                                    "Saved trace exceeds its declared capture capacity",
                                )
                            fields = {
                                key: item[key]
                                for key in SCHEMAS[table]
                                if key not in {*stage_base, "ordinal", "payload"}
                            }
                            append(
                                table,
                                {
                                    **stage_base,
                                    "ordinal": ordinal,
                                    **fields,
                                    "payload": _json(item),
                                },
                            )
            if (
                decisions != summary.decisions_saved
                or details != summary.detail_rows_saved
            ):
                raise TraceStorageError(
                    "trace_count_mismatch",
                    "Saved trace rows differ from the declared runtime capture counters",
                )
            for table in TABLES:
                flush(table)
        manifest = TraceManifest.model_validate(
            {
                **summary.model_dump(mode="json"),
                "artifacts": [ref.model_dump(mode="json") for ref in refs],
            }
        )
        publish(
            manifest_name(binding.evaluation_id),
            canonical_bytes(manifest),
            "application/json",
            1,
        )
        return manifest, artifacts
    except TraceStorageError:
        raise
    except (OSError, ValueError, TypeError, duckdb.Error):
        raise TraceStorageError() from None


def _table_ref(manifest, ref, table):
    prefix = f"evaluation-{manifest.binding.evaluation_id}-trace-{table}-"
    return ref.name.startswith(prefix) and ref.name.endswith(".parquet")


@contextmanager
def _verified_file(path, ref):
    """Pin every path component and the inode through hashing and DuckDB reads."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise TraceStorageError()
    fd = None
    directory = None
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != ref.size_bytes:
            raise TraceStorageError()
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != ref.sha256:
            raise TraceStorageError()
        # /proc preserves the verified open inode even if its pathname is renamed.
        yield f"/proc/self/fd/{fd}"
    except FileNotFoundError:
        raise TraceStorageError(
            "trace_artifact_missing",
            "Trace evidence is unavailable; restore its matching storage backup",
        ) from None
    except OSError:
        raise TraceStorageError() from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


class TraceReader:
    def __init__(
        self,
        manifest: TraceManifest,
        paths: Mapping[str, Path],
        *,
        max_scan_bytes=MAX_SCAN_BYTES,
    ):
        self.manifest, self.paths, self.max_scan_bytes = manifest, paths, max_scan_bytes
        if len({ref.name for ref in manifest.artifacts}) != len(manifest.artifacts):
            raise TraceStorageError()
        for ref in manifest.artifacts:
            if (
                ref.media_type != "application/vnd.apache.parquet"
                or ref.date_range is None
                or not any(_table_ref(manifest, ref, table) for table in TABLES)
            ):
                raise TraceStorageError()

    def query(
        self,
        table,
        *,
        start=None,
        end=None,
        decision_id=None,
        stage_id=None,
        instrument_id=None,
        record_ordinal=None,
        eligibility=None,
        reason=None,
        rank_min=None,
        rank_max=None,
        score_min=None,
        score_max=None,
        offset=0,
        limit=100,
    ):
        """A fixed projection and parameterized predicates; never user SQL/paths.

        total is the exact filtered count before pagination. Empty pages retain
        it, including offsets beyond the last result. Nonmatching dates prune
        files before hashing/opening. Detail queries require a resolved event.
        """
        from contextlib import ExitStack

        if (
            table not in TABLES
            or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE
            or type(offset) is not int
            or not 0 <= offset <= MAX_OFFSET
        ):
            raise TraceStorageError(
                "invalid_trace_query", "Invalid trace pagination bounds"
            )
        if start is not None and end is not None and start >= end:
            raise TraceStorageError(
                "invalid_trace_query", "Trace start must precede exclusive end"
            )
        if table != "events" and (decision_id is None or start is None or end is None):
            raise TraceStorageError(
                "invalid_trace_query",
                "Trace detail requires an exact resolved decision",
            )
        if (
            eligibility
            not in {None, "eligible", "ineligible", "unknown", "not_sampled"}
            or (rank_min is not None and rank_min < 1)
            or (rank_max is not None and rank_max < 1)
            or (rank_min is not None and rank_max is not None and rank_min > rank_max)
            or any(
                value is not None
                and (type(value) not in (int, float) or not math.isfinite(value))
                for value in (score_min, score_max)
            )
            or (
                score_min is not None
                and score_max is not None
                and score_min > score_max
            )
        ):
            raise TraceStorageError(
                "invalid_trace_query", "Invalid trace candidate filters"
            )
        if record_ordinal is not None and (
            table not in {"features", "predicates", "candidates"}
            or stage_id is None
            or type(record_ordinal) is not int
            or not 0 <= record_ordinal <= 2**63 - 1
        ):
            raise TraceStorageError(
                "invalid_trace_query",
                "A recorded ordinal requires a stage and a nonnegative integer within a detail table",
            )
        refs = [
            ref
            for ref in self.manifest.artifacts
            if _table_ref(self.manifest, ref, table)
            and (start is None or ref.date_range.end > start)
            and (end is None or ref.date_range.start < end)
        ]
        if (
            sum(ref.size_bytes for ref in refs) > self.max_scan_bytes
            or len(refs) > 4096
        ):
            raise TraceStorageError(
                "trace_query_too_large",
                "Narrow the trace date window; selected blocks exceed the scan budget",
            )
        conditions, values = [], []
        for column, operation, value in (
            ("session_date", ">=", start),
            ("session_date", "<", end),
            ("decision_id", "=", decision_id),
            ("stage_id", "=", stage_id),
            ("instrument_id", "=", instrument_id),
            ("ordinal", "=", record_ordinal),
            ("eligibility", "=", eligibility),
            ("reason", "=", reason),
            ("rank", ">=", rank_min),
            ("rank", "<=", rank_max),
            ("score", ">=", score_min),
            ("score", "<=", score_max),
        ):
            if value is None:
                continue
            if column == "stage_id" and table == "events":
                conditions.append("list_contains(CAST(stage_ids AS VARCHAR[]), ?)")
            elif column not in SCHEMAS[table]:
                raise TraceStorageError(
                    "invalid_trace_query", "Unsupported filter for this trace section"
                )
            else:
                conditions.append(f'"{column}" {operation} ?')
            values.append(value)
        if not refs:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        where = " AND ".join(conditions) or "true"
        order = (
            "decision_time, event_seq, decision_id"
            if table == "events"
            else "stage_id, ordinal, instrument_id"
            if table in {"candidates", "features", "predicates"}
            else "decision_id"
            if table == "decisions"
            else "stage_id"
        )
        projection = ",".join(
            f"CAST(\"{key}\" AT TIME ZONE 'UTC' AS VARCHAR) || '+00:00' AS \"{key}\""
            if kind == "TIMESTAMPTZ"
            else f'"{key}"'
            for key, kind in SCHEMAS[table].items()
        )
        try:
            with ExitStack() as stack:
                paths = [
                    stack.enter_context(_verified_file(self.paths[ref.name], ref))
                    for ref in refs
                ]
                connection = stack.enter_context(_connection())
                total = connection.execute(
                    f"SELECT count(*) FROM read_parquet(?) WHERE {where}",
                    [paths, *values],
                ).fetchone()[0]
                # Reject an oversized page BEFORE materializing payload strings.
                if "payload" in SCHEMAS[table]:
                    page_bytes, largest_row = connection.execute(
                        f"SELECT coalesce(sum(octet_length(encode(payload))), 0), coalesce(max(octet_length(encode(payload))), 0) FROM (SELECT payload FROM read_parquet(?) WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?)",
                        [paths, *values, limit, offset],
                    ).fetchone()
                    if largest_row > MAX_RESPONSE_BYTES:
                        raise TraceStorageError(
                            "trace_detail_too_large",
                            "An individual recorded observation exceeds the browser detail limit; its exact bytes remain in the immutable downloadable Parquet artifact",
                        )
                    if page_bytes > MAX_RESPONSE_BYTES:
                        raise TraceStorageError(
                            "trace_query_too_large",
                            "Narrow the stage/instrument selection or page size; selected detail exceeds the response budget",
                        )
                # Predicate/projection pushdown occurs before the bounded fetch.
                result = connection.execute(
                    f"SELECT {projection} FROM read_parquet(?) WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                    [paths, *values, limit, offset],
                )
                keys = [entry[0] for entry in result.description]
                rows = [dict(zip(keys, row)) for row in result.fetchall()]
                if (
                    sum(len(row.get("payload", "")) for row in rows)
                    > MAX_RESPONSE_BYTES
                ):
                    raise TraceStorageError(
                        "trace_query_too_large",
                        "Narrow the stage/instrument selection or page size; selected detail exceeds the response budget",
                    )
                for row in rows:
                    if "payload" in row:
                        row["payload"] = json.loads(row["payload"])
                    if "stage_ids" in row:
                        row["stage_ids"] = json.loads(row["stage_ids"])
                return {"items": rows, "total": total, "offset": offset, "limit": limit}
        except TraceStorageError:
            raise
        except (KeyError, ValueError, TypeError, duckdb.Error):
            raise TraceStorageError() from None
