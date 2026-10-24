"""Execute the frozen synthetic native trace capacity protocol, never a backtest.

The normal command always retains 1,000 listings × 750 sessions × 3 variants.
Each phase runs in a separate process, with its own actual peak RSS. All reader
calls use production bounds and integrity verification. No PostgreSQL, HTTP,
portable ABI, live provider, or native-engine latency is claimed here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import resource
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.research_trace.comparison import (
    ComparisonControl,
    ComparisonReader,
    compare_evaluations,
)
from packages.research_trace.comparison_codec import (
    PAYLOAD_ACCOUNTING_VERSION,
    PAYLOAD_CODEC,
    decode_record,
)
from packages.research_trace.comparison_contracts import (
    DEFAULT_LIMITS,
    ComparisonInput,
    ComparisonManifest,
    ComparisonPublication,
    EventNamespace,
)
from packages.research_trace.comparison_storage import _OUTPUT_SCHEMA
from packages.research_trace.contracts import (
    DateRange,
    EvaluationBinding,
    TraceManifest,
    canonical_bytes,
    digest,
)
from packages.research_trace.storage import (
    SCHEMAS,
    TABLES,
    TraceReader,
    _connection,
    _table_ref,
    _verified_file,
)
from script.research_trace_benchmark_fixture import (
    FIXTURE_VERSION,
    FROZEN_LISTINGS,
    FROZEN_SESSIONS,
    frozen_namespace,
    trace_shaped_input,
)

REPORT_VERSION = "tradegrad.trace-capacity-report.v1"
BUDGET_VERSION = "tradegrad.trace-capacity-budget.v1"
SAMPLES = 30
AUDIT_DEADLINE_SECONDS = 900
AUDIT_RECORD_BYTES = 64 * 1024
REFERENCE = {
    "platform": "Linux x86_64",
    "python": "3.12",
    "dedicated_logical_cpus": 2,
    "memory_budget_bytes": 7 * 1024**3,
    "filesystem": "local SSD",
    "postgresql": "18.6",
}
BOUNDARY = (
    "Synthetic native trace-shaped storage/query/comparison only. No strategy or "
    "market/backtest/economic execution; no portable ABI, HTTP, PostgreSQL or "
    "browser latency. First-open means a new application reader and manifest "
    "handle, not an OS page-cache flush. Hot means a reused application reader; "
    "production DuckDB connections and integrity hashes are still recreated."
)


def require(condition, category="invalid_report"):
    if not condition:
        raise ValueError(category)


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 2 * 1024**2,
            "invalid_json_size",
        )
        raw = stream.read(2 * 1024**2 + 1)
    require(0 < len(raw) <= 2 * 1024**2, "invalid_json_size")

    def pairs(items):
        output = {}
        for key, value in items:
            require(key not in output, "duplicate_json_key")
            output[key] = value
        return output

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
    )


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive publication: never replace another run's evidence.
    with path.open("xb") as stream:
        stream.write(canonical_bytes(value) + b"\n")


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024**2):
            result.update(block)
    return result.hexdigest()


def command(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def source_evidence():
    files = sorted(
        [*ROOT.glob("packages/research_trace/*.py")]
        + [
            ROOT / "script/research_trace_benchmark.py",
            ROOT / "script/research_trace_benchmark_fixture.py",
            ROOT / "backend/app/core/atomic_storage.py",
            ROOT / "backend/requirements.lock",
        ]
    )
    hashes = {str(path.relative_to(ROOT)): sha(path) for path in files}
    dirty = command(["git", "status", "--porcelain", "--untracked-files=normal"])
    return {
        "parent_head": command(["git", "rev-parse", "HEAD"]),
        "dirty": bool(dirty),
        "dirty_paths": dirty.splitlines() if dirty else [],
        "relevant_files_sha256": hashes,
        "relevant_tree_sha256": digest(hashes),
        "identity_note": "A dirty measurement is parent HEAD plus exact file hashes, not final-commit certification.",
    }


def environment_evidence(work):
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    versions = {}
    for package in ("duckdb", "pydantic", "fastapi", "psycopg"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    memory = read("/proc/meminfo") or ""
    cpu = read("/proc/cpuinfo") or ""
    return {
        "reference": REFERENCE,
        "reference_equivalence": "not_asserted: CPU dedication and SSD media are not established; PostgreSQL is not exercised",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host_node_sha256": hashlib.sha256(platform.node().encode()).hexdigest(),
        "python": platform.python_version(),
        "packages": versions,
        "logical_cpus_visible": os.cpu_count(),
        "logical_cpus_affinity": len(os.sched_getaffinity(0)),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in cpu.splitlines()
                if line.startswith("model name")
            ),
            None,
        ),
        "host_memory_total": next(
            (line for line in memory.splitlines() if line.startswith("MemTotal:")), None
        ),
        "cgroup": {
            name: read(f"/sys/fs/cgroup/{name}")
            for name in (
                "cpu.max",
                "cpu.weight",
                "memory.max",
                "memory.high",
                "cpuset.cpus.effective",
            )
        },
        "filesystem": command(["findmnt", "-T", str(work), "-n", "-o", "FSTYPE"]),
        "storage_media": "not established; overlay mount does not certify local SSD",
        "cpu_dedication": "not established",
        "disk_free_bytes_at_start": os.statvfs(work).f_bavail
        * os.statvfs(work).f_frsize,
        "postgresql_server": "not_measured",
        "postgresql_client": command(
            ["sh", "-c", "command -v psql >/dev/null && psql --version"]
        ),
        "duckdb_limits": {
            "threads": 1,
            "memory": "256MiB comparison / 256MB trace",
            "spill": "0B",
            "comparison": DEFAULT_LIMITS.model_dump(mode="json"),
        },
    }


def load_input(work, variant):
    data = read_json(work / f"variant-{variant}" / "input.json")
    manifest_path = work / f"variant-{variant}" / data["manifest_name"]
    require(sha(manifest_path) == data["manifest_sha256"], "trace_manifest_integrity")
    manifest = TraceManifest.model_validate_json(manifest_path.read_bytes())
    require(
        digest(manifest) == data["trace_manifest_sha256"], "trace_manifest_integrity"
    )
    return ComparisonInput(
        binding=EvaluationBinding.model_validate(data["binding"]),
        source_kind="strategy_version",
        source_id=manifest.binding.strategy_version_id,
        source=data["source"],
        window=DateRange.model_validate(data["window"]),
        spec=None,
        spec_sha256=None,
        trace_manifest=manifest,
        trace_manifest_sha256=data["trace_manifest_sha256"],
        trace_paths={
            ref.name: manifest_path.parent / ref.name for ref in manifest.artifacts
        },
        common_config=data["common_config"],
        economic_bundle={},
        namespace=EventNamespace.model_validate(data["namespace"]),
    )


def collect(work, variant, sessions):
    namespace = EventNamespace.model_validate(read_json(work / "namespace.json"))
    value = trace_shaped_input(
        work / f"variant-{variant}", namespace, sessions=sessions, variant=variant
    )
    manifest = value.trace_manifest
    manifest_name = f"evaluation-{value.binding.evaluation_id}-trace-manifest.json"
    write_json(
        work / f"variant-{variant}" / "input.json",
        {
            "binding": value.binding.model_dump(mode="json"),
            "source": value.source,
            "window": value.window.model_dump(mode="json"),
            "manifest_name": manifest_name,
            "manifest_sha256": sha(work / f"variant-{variant}" / manifest_name),
            "trace_manifest_sha256": value.trace_manifest_sha256,
            "common_config": dict(value.common_config),
            "namespace": namespace.model_dump(mode="json"),
        },
    )
    counts = {
        table: sum(
            ref.rows for ref in manifest.artifacts if f"-trace-{table}-" in ref.name
        )
        for table in TABLES
    }
    expected = {
        table: sessions
        * (FROZEN_LISTINGS if table in {"candidates", "features", "predicates"} else 1)
        for table in TABLES
    }
    require(counts == expected, "trace_table_counts")
    require(
        manifest.coverage == "complete"
        and manifest.decisions_seen == manifest.decisions_saved == sessions
        and manifest.detail_rows_seen
        == manifest.detail_rows_saved
        == 3 * FROZEN_LISTINGS * sessions,
        "trace_coverage",
    )
    return {
        "variant": variant,
        "coverage": manifest.coverage,
        "missing_reasons": manifest.missing_reasons,
        "decisions_seen": manifest.decisions_seen,
        "decisions_saved": manifest.decisions_saved,
        "detail_rows_seen": manifest.detail_rows_seen,
        "detail_rows_saved": manifest.detail_rows_saved,
        "table_rows": counts,
        "artifact_count": len(manifest.artifacts) + 1,
        "compressed_artifact_bytes": sum(ref.size_bytes for ref in manifest.artifacts)
        + (work / f"variant-{variant}" / manifest_name).stat().st_size,
        "manifest_sha256": value.trace_manifest_sha256,
        "capture": manifest.capture.model_dump(mode="json"),
    }


def timings(samples):
    require(
        len(samples) >= SAMPLES
        and all(
            type(item) in (int, float) and math.isfinite(item) and item > 0
            for item in samples
        ),
        "invalid_samples",
    )
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "count": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_ms": max(samples),
    }


def audit(work, variant):
    value = load_input(work, variant)
    manifest = value.trace_manifest
    TraceReader(manifest, value.trace_paths)
    output = {}
    with ExitStack() as stack, _connection() as connection:
        pinned = {
            ref.name: stack.enter_context(
                _verified_file(value.trace_paths[ref.name], ref)
            )
            for ref in manifest.artifacts
        }
        for table in TABLES:
            paths = [
                pinned[ref.name]
                for ref in manifest.artifacts
                if _table_ref(manifest, ref, table)
            ]
            count, sessions, decisions = connection.execute(
                "SELECT count(*), count(DISTINCT session_date), count(DISTINCT decision_id) FROM read_parquet(?)",
                [paths],
            ).fetchone()
            expected = FROZEN_SESSIONS * (
                FROZEN_LISTINGS
                if table in {"candidates", "features", "predicates"}
                else 1
            )
            require(
                count == expected and sessions == decisions == FROZEN_SESSIONS,
                "physical_parquet_coverage",
            )
            detail = {"rows": count, "sessions": sessions, "decisions": decisions}
            if table in {"candidates", "features", "predicates"}:
                # Count every saved instrument in every event. No paging, sampling,
                # or approximate cardinality can certify full-pool preservation.
                rows = connection.execute(
                    "SELECT count(*), min(n), max(n), min(symbols), max(symbols) FROM (SELECT decision_id, count(*) AS n, count(DISTINCT instrument_id) AS symbols FROM read_parquet(?) GROUP BY decision_id)",
                    [paths],
                ).fetchone()
                require(
                    rows
                    == (
                        FROZEN_SESSIONS,
                        FROZEN_LISTINGS,
                        FROZEN_LISTINGS,
                        FROZEN_LISTINGS,
                        FROZEN_LISTINGS,
                    ),
                    "physical_candidate_pool",
                )
                symbols = connection.execute(
                    "SELECT DISTINCT instrument_id FROM read_parquet(?) ORDER BY instrument_id LIMIT 1001",
                    [paths],
                ).fetchall()
                require(
                    [symbol[0] for symbol in symbols]
                    == [f"S{i:04d}" for i in range(FROZEN_LISTINGS)],
                    "physical_frozen_universe",
                )
                detail.update(
                    {
                        "exact_frozen_instrument_set": True,
                        "min_rows_per_event": rows[1],
                        "max_rows_per_event": rows[2],
                        "min_instruments_per_event": rows[3],
                        "max_instruments_per_event": rows[4],
                    }
                )
            output[table] = detail
    return {
        "variant": variant,
        "physical_tables": output,
        "all_artifact_hashes_verified": True,
        "manifest_sha256": value.trace_manifest_sha256,
    }


def query_shape(reader, shape, event):
    resolved = reader.query("events", decision_id=event["decision_id"], limit=2)
    require(resolved["total"] == len(resolved["items"]) == 1, "resolved_event")
    start = resolved["items"][0]["session_date"]
    filters = {
        "start": start,
        "end": start + timedelta(days=1),
        "decision_id": event["decision_id"],
        "stage_id": "ranking",
    }
    if shape == "candidate_page50":
        page = reader.query("candidates", **filters, offset=0, limit=50)
        require(
            page["total"] == FROZEN_LISTINGS and len(page["items"]) == 50,
            "candidate_query_rows",
        )
        require(
            [row["instrument_id"] for row in page["items"]]
            == [f"S{i:04d}" for i in range(50)],
            "candidate_query_content",
        )
    else:
        header = reader.query(
            "decisions",
            start=start,
            end=start + timedelta(days=1),
            decision_id=event["decision_id"],
            limit=2,
        )
        stage = reader.query("stages", **filters, limit=100)
        require(header["total"] == stage["total"] == 1, "detail_header_stage")
        require(
            stage["items"][0]["payload"]["features_saved"]
            == stage["items"][0]["payload"]["predicates_saved"]
            == FROZEN_LISTINGS,
            "detail_stage_count",
        )
        for table in ("features", "predicates"):
            page = reader.query(table, **filters, instrument_id="S0450", limit=50)
            require(page["total"] == len(page["items"]) == 1, "detail_query_rows")
            row = page["items"][0]["payload"]
            if table == "features":
                require(
                    row["value"] == 550.0 and row["validity"] == "valid",
                    "detail_feature",
                )
            if table == "predicates":
                require(
                    row["operands"] == [550.0]
                    and row["threshold"] == 0
                    and row["state"] == "true",
                    "detail_predicate",
                )


def queries(work, variant):
    value = load_input(work, variant)
    reader = TraceReader(value.trace_manifest, value.trace_paths)
    first = reader.query("events", offset=0, limit=500)
    second = reader.query("events", offset=500, limit=500)
    events = first["items"] + second["items"]
    require(
        first["total"] == second["total"] == len(events) == FROZEN_SESSIONS,
        "query_sample_events",
    )
    results = {}
    for shape in ("candidate_page50", "instrument_stage_detail"):
        for mode in ("first_open", "hot"):
            samples = []
            for index in range(SAMPLES):
                # Distribute queries across the indexed observation window.
                event = events[(index * (len(events) - 1)) // (SAMPLES - 1)]
                started = time.perf_counter()
                if mode == "first_open":
                    fresh = load_input(work, variant)
                    selected = TraceReader(fresh.trace_manifest, fresh.trace_paths)
                else:
                    selected = reader
                query_shape(selected, shape, event)
                samples.append((time.perf_counter() - started) * 1000)
            results[f"{shape}.{mode}"] = timings(samples)
    return {
        "variant": variant,
        "queries": results,
        "boundary": BOUNDARY,
        "candidate_offset": 0,
        "candidate_limit": 50,
        "detail_sections": ["events", "decisions", "stages", "features", "predicates"],
    }


class CapacityAuditControl(ComparisonControl):
    """Separate finite ownership for verification, never comparison latency."""

    def __init__(self):
        super().__init__(timeout_seconds=AUDIT_DEADLINE_SECONDS)
        self.source_bytes = 0

    def account_source(self, row):
        self.check()
        size = sum(len(str(value).encode()) for value in row.values())
        require(size <= AUDIT_RECORD_BYTES, "audit_source_record_bytes")
        self.source_bytes += size
        require(
            self.source_bytes <= DEFAULT_LIMITS.max_input_bytes,
            "audit_source_byte_budget",
        )


def audit_physical_schema(connection, pinned, expected):
    actual = {
        row[0]: row[1]
        for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [pinned]
        ).fetchall()
    }
    normalized = {
        key: "TIMESTAMP WITH TIME ZONE" if value == "TIMESTAMPTZ" else value
        for key, value in expected.items()
    }
    require(actual == normalized, "audit_physical_schema")


def source_rows(value, table, control):
    """Independent immutable source scan, one bounded physical block at a time."""
    require(table in TABLES, "audit_table")
    refs = sorted(
        (
            ref
            for ref in value.trace_manifest.artifacts
            if _table_ref(value.trace_manifest, ref, table)
        ),
        key=lambda ref: (ref.date_range.start, ref.name),
    )
    require(
        0 < value.trace_manifest.decisions_saved <= FROZEN_SESSIONS,
        "audit_source_event_budget",
    )
    expected = value.trace_manifest.decisions_saved * (
        FROZEN_LISTINGS if table in {"candidates", "features", "predicates"} else 1
    )
    require(
        sum(ref.rows for ref in refs) == expected
        and len(refs) <= DEFAULT_LIMITS.max_files
        and sum(ref.size_bytes for ref in refs) <= DEFAULT_LIMITS.max_input_bytes,
        "audit_source_manifest_bounds",
    )
    order = [
        key
        for key in ("session_date", "decision_id", "stage_id", "ordinal")
        if key in SCHEMAS[table]
    ]
    with control.connection() as connection:
        previous = None
        total = 0
        for ref in refs:
            with _verified_file(value.trace_paths[ref.name], ref) as pinned:
                audit_physical_schema(connection, pinned, SCHEMAS[table])
                sizes = "+".join(
                    f"coalesce(octet_length(encode({key})),0)"
                    if kind == "VARCHAR"
                    else "64"
                    for key, kind in SCHEMAS[table].items()
                )
                physical_count, largest = connection.execute(
                    f"SELECT count(*),max({sizes}) FROM read_parquet(?)", [pinned]
                ).fetchone()
                require(
                    physical_count == ref.rows and 0 < largest <= AUDIT_RECORD_BYTES,
                    "audit_source_physical_bounds",
                )
                batch_size = max(1, min(256, 4 * 1024**2 // largest))
                projection = ",".join(
                    f"CAST({key} AS VARCHAR) AS {key}" if kind == "TIMESTAMPTZ" else key
                    for key, kind in SCHEMAS[table].items()
                )
                cursor = connection.execute(
                    f"SELECT {projection} FROM read_parquet(?) ORDER BY {','.join(order)}",
                    [pinned],
                )
                columns = [column[0] for column in cursor.description]
                seen = 0
                while batch := cursor.fetchmany(batch_size):
                    for values in batch:
                        control.check()
                        row = dict(zip(columns, values, strict=True))
                        control.account_source(row)
                        key = tuple(row[field] for field in order)
                        require(
                            previous is None or key > previous, "source_stream_order"
                        )
                        previous = key
                        seen += 1
                        total += 1
                        require(total <= expected, "audit_source_row_budget")
                        yield row
                require(seen == ref.rows, "source_stream_count")


def source_event_headers(value, control):
    """Only the fixed 750 event headers/stages are indexed, never detail rows."""

    def metadata_rows(table):
        with closing(source_rows(value, table, control)) as stream:
            return list(stream)

    stages = {}
    for row in metadata_rows("stages"):
        require(row["stage_id"] == "ranking", "source_fixture_stage")
        require(row["decision_id"] not in stages, "duplicate_source_stage")
        stages[row["decision_id"]] = json.loads(row["payload"])
    headers = {}
    for row in metadata_rows("decisions"):
        body = json.loads(row["payload"])
        stage = stages[row["decision_id"]]
        body.update(
            session_date=row["session_date"].isoformat(),
            stage_coverage=[
                {
                    "stage_id": "ranking",
                    "coverage": stage["coverage"],
                    "missing_reason": stage["missing_reason"],
                }
            ],
        )
        require(body["decision_id"] == row["decision_id"], "source_header_identity")
        headers[body["session_date"]] = body
    events = metadata_rows("events")
    require(
        len(headers)
        == len(stages)
        == len(events)
        == value.trace_manifest.decisions_saved,
        "source_event_count",
    )
    for event in events:
        header = headers[event["session_date"].isoformat()]
        require(
            all(
                header[key] == event[key]
                for key in (
                    "decision_id",
                    "data_event_id",
                    "callback_phase",
                    "scope",
                    "event_seq",
                    "instrument_id",
                )
            ),
            "source_event_identity",
        )
        require(json.loads(event["stage_ids"]) == ["ranking"], "source_event_stages")
    return headers, stages


def full_comparison_audit(manifest, paths, left, right, *, control=None):
    control = control or CapacityAuditControl()
    try:
        result = _full_comparison_audit(manifest, paths, left, right, control)
    finally:
        control.close()
        require(
            control.closure_confirmed and control.active_connections == 0,
            "audit_connection_leak",
        )
    result.update(
        deadline_seconds=AUDIT_DEADLINE_SECONDS,
        closure_confirmed=True,
        source_scan_bytes=control.source_bytes,
        source_byte_budget=DEFAULT_LIMITS.max_input_bytes,
        fixture_record_byte_budget=AUDIT_RECORD_BYTES,
    )
    return result


def _full_comparison_audit(manifest, paths, left, right, control):
    """Full lossless round-trip plus source-equivalence proof, outside query timing.

    The golden expansion is intentionally independent of the production decoder.
    Every original nested observation is compared byte-for-byte with its pinned
    source payload. Streaming hashes bind identities, ordinals and observations.
    No comparison output cap, page cap or deadline is disabled for this audit.
    """
    started = time.perf_counter()
    control.check()
    require(
        sum(ref.rows for ref in manifest.artifacts)
        <= FROZEN_SESSIONS * (3 * FROZEN_LISTINGS + 5) + 16
        and sum(ref.size_bytes for ref in manifest.artifacts)
        <= DEFAULT_LIMITS.max_output_bytes
        and len(manifest.artifacts) <= DEFAULT_LIMITS.max_files,
        "audit_output_manifest_bounds",
    )
    headers, stages = zip(
        *(source_event_headers(value, control) for value in (left, right)), strict=True
    )
    require(headers[0].keys() == headers[1].keys(), "source_event_window")
    pairs = {
        digest(
            {
                "input_hash": manifest.input_hash,
                "left": headers[0][day]["decision_id"],
                "right": headers[1][day]["decision_id"],
            }
        ): day
        for day in headers[0]
    }
    detail_sections = {"candidates", "features", "predicates"}
    output = {}
    total_stored = total_expanded = total_rows = 0
    for section, summary in manifest.sections.items():
        refs = sorted(
            (ref for ref in manifest.artifacts if ref.section == section),
            key=lambda ref: ref.first_row,
        )
        expanded_hash = hashlib.sha256()
        source_hashes = [hashlib.sha256(), hashlib.sha256()]
        restored_hashes = [hashlib.sha256(), hashlib.sha256()]
        side_counts = [0, 0]
        event_ids = set()
        target_seen = [set(), set()]
        count = stored_bytes = expanded_bytes = 0
        with ExitStack() as stack, control.connection() as connection:
            streams = []
            if section in detail_sections:
                for value in (left, right):
                    stream = source_rows(value, section, control)
                    stack.callback(stream.close)
                    streams.append(stream)
            for ref in refs:
                with _verified_file(paths[ref.name], ref) as pinned:
                    audit_physical_schema(connection, pinned, _OUTPUT_SCHEMA)
                    physical_count, largest = connection.execute(
                        "SELECT count(*),max(greatest(expanded_payload_bytes,128+octet_length(encode(payload))+coalesce(octet_length(encode(event_pair_id)),0)+coalesce(octet_length(encode(session_date)),0)+coalesce(octet_length(encode(instrument_id)),0)+coalesce(octet_length(encode(stage_id)),0)+coalesce(octet_length(encode(status)),0))) FROM read_parquet(?)",
                        [pinned],
                    ).fetchone()
                    require(
                        physical_count == ref.rows
                        and 0 < largest <= AUDIT_RECORD_BYTES,
                        "comparison_expanded_record_bound",
                    )
                    batch_size = max(1, min(256, 4 * 1024**2 // largest))
                    cursor = connection.execute(
                        "SELECT row_number,payload,expanded_payload_bytes,event_pair_id,session_date,instrument_id,stage_id,status FROM read_parquet(?) ORDER BY row_number",
                        [pinned],
                    )
                    block_count = 0
                    while batch := cursor.fetchmany(batch_size):
                        for (
                            number,
                            payload,
                            declared,
                            pair_id,
                            day,
                            instrument,
                            stage,
                            status,
                        ) in batch:
                            control.check()
                            require(number == count, "comparison_stream_order")
                            projected = dict(
                                zip(
                                    (
                                        "section",
                                        "event_pair_id",
                                        "session_date",
                                        "instrument_id",
                                        "stage_id",
                                        "status",
                                    ),
                                    (section, pair_id, day, instrument, stage, status),
                                    strict=True,
                                )
                            )
                            envelope = json.loads(payload)
                            require(
                                type(envelope) is list
                                and len(envelope) == 2
                                and envelope[0] == "tradegrad.comparison-row.v2",
                                "comparison_codec_version",
                            )
                            golden = {
                                "ambiguous": False,
                                "associations": [],
                                "missing_reason": None,
                                **envelope[1],
                                **projected,
                            }
                            golden_bytes = canonical_bytes(golden)
                            record, actual_size = decode_record(
                                payload, projected, declared
                            )
                            require(
                                canonical_bytes(record) == golden_bytes
                                and actual_size == declared == len(golden_bytes),
                                "comparison_roundtrip",
                            )
                            expanded_hash.update(golden_bytes + b"\n")
                            count += 1
                            block_count += 1
                            stored_bytes += len(payload.encode())
                            expanded_bytes += declared
                            require(
                                total_stored + stored_bytes
                                <= DEFAULT_LIMITS.max_output_bytes
                                and total_rows + count
                                <= FROZEN_SESSIONS * (3 * FROZEN_LISTINGS + 5) + 16,
                                "audit_output_stream_bounds",
                            )
                            if section in {"events", "targets"} | detail_sections:
                                require(
                                    pair_id in pairs and pairs[pair_id] == day,
                                    "comparison_event_identity",
                                )
                                event_ids.add(pair_id)
                            for side, body in enumerate(
                                (golden["left"], golden["right"])
                            ):
                                if section == "events":
                                    source_packet = (
                                        canonical_bytes(headers[side][day]) + b"\n"
                                    )
                                    restored_packet = canonical_bytes(body) + b"\n"
                                    require(
                                        source_packet == restored_packet,
                                        "comparison_full_event_header",
                                    )
                                    side_counts[side] += 1
                                elif section in detail_sections:
                                    source = next(streams[side], None)
                                    require(
                                        source is not None
                                        and body is not None
                                        and type(body["count"]) is int
                                        and body["count"] == len(body["items"]) == 1,
                                        "comparison_full_observation_count",
                                    )
                                    item = body["items"][0]
                                    identity = [
                                        day,
                                        headers[side][day]["decision_id"],
                                        stage,
                                        item["ordinal"],
                                    ]
                                    source_identity = [
                                        source["session_date"].isoformat(),
                                        source["decision_id"],
                                        source["stage_id"],
                                        source["ordinal"],
                                    ]
                                    require(
                                        canonical_bytes(identity)
                                        == canonical_bytes(source_identity),
                                        "comparison_full_observation_identity",
                                    )
                                    require(
                                        instrument
                                        == source["instrument_id"]
                                        == item["record"]["instrument_id"]
                                        == f"S{source['ordinal']:04d}",
                                        "comparison_full_instrument",
                                    )
                                    original = source["payload"].encode()
                                    restored = canonical_bytes(item["record"])
                                    require(
                                        original == restored,
                                        "comparison_full_observation_bytes",
                                    )
                                    source_hashes[side].update(
                                        canonical_bytes(source_identity)
                                        + b"\n"
                                        + original
                                        + b"\n"
                                    )
                                    restored_hashes[side].update(
                                        canonical_bytes(identity)
                                        + b"\n"
                                        + restored
                                        + b"\n"
                                    )
                                    side_counts[side] += 1
                                    continue
                                elif section == "targets":
                                    if body is None:
                                        continue
                                    require(
                                        type(body["count"]) is int
                                        and body["count"] == len(body["items"]) == 1,
                                        "comparison_target_count",
                                    )
                                    item = body["items"][0]
                                    require(
                                        type(item["ordinal"]) is int
                                        and item["ordinal"] == 0
                                        and item["target_kind"]
                                        in {"raw_targets", "constrained_plan"},
                                        "comparison_target_ordinal",
                                    )
                                    key = (
                                        pair_id,
                                        item["target_kind"],
                                        item["ordinal"],
                                    )
                                    require(
                                        key not in target_seen[side]
                                        and stage == "ranking",
                                        "comparison_target_identity",
                                    )
                                    target_seen[side].add(key)
                                    expected = stages[side][
                                        headers[side][day]["decision_id"]
                                    ][item["target_kind"]][item["ordinal"]]
                                    require(
                                        canonical_bytes(item["record"])
                                        == canonical_bytes(expected)
                                        and instrument == expected["instrument_id"],
                                        "comparison_target_observation",
                                    )
                                    side_counts[side] += 1
                                    source_packet = (
                                        canonical_bytes([key, expected]) + b"\n"
                                    )
                                    restored_packet = (
                                        canonical_bytes([key, item["record"]]) + b"\n"
                                    )
                                else:
                                    continue
                                source_hashes[side].update(source_packet)
                                restored_hashes[side].update(restored_packet)
                    require(block_count == ref.rows, "comparison_block_count")
            for stream in streams:
                require(next(stream, None) is None, "comparison_unconsumed_source")
        require(count == summary.rows, "comparison_physical_count")
        if section in {"events", "targets"} | detail_sections:
            expected = len(pairs) * (
                FROZEN_LISTINGS
                if section in detail_sections
                else 2
                if section == "targets"
                else 1
            )
            require(
                side_counts == [expected, expected] and len(event_ids) == len(pairs),
                "comparison_complete_source_coverage",
            )
        output[section] = {
            "rows": count,
            "expanded_canonical_sha256": expanded_hash.hexdigest(),
            "stored_payload_bytes": stored_bytes,
            "expanded_payload_bytes": expanded_bytes,
            "event_pairs": len(event_ids),
            "side_observation_counts": side_counts,
            "source_observations_sha256": [
                value.hexdigest() for value in source_hashes
            ],
            "restored_observations_sha256": [
                value.hexdigest() for value in restored_hashes
            ],
        }
        total_rows += count
        total_stored += stored_bytes
        total_expanded += expanded_bytes
    require(total_stored <= DEFAULT_LIMITS.max_output_bytes, "comparison_output_bytes")
    control.check()
    return {
        "codec": PAYLOAD_CODEC,
        "payload_accounting_version": PAYLOAD_ACCOUNTING_VERSION,
        "all_rows_roundtripped": True,
        "all_source_observations_equivalent": True,
        "sections": output,
        "source_manifest_sha256": [
            left.trace_manifest_sha256,
            right.trace_manifest_sha256,
        ],
        "elapsed_seconds": time.perf_counter() - started,
        "physical_output_rows": total_rows,
        "uncompressed_payload_bytes": total_stored,
        "expanded_payload_bytes": total_expanded,
    }


def comparison(work, variant):
    left, right = load_input(work, 0), load_input(work, variant)
    target = work / f"comparison-0-{variant}-{uuid4()}"
    publication = ComparisonPublication(
        comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
    )
    # The production default 300-second deadline is retained, not removed.
    control = ComparisonControl(timeout_seconds=300)
    started = time.perf_counter()
    manifest, artifacts = compare_evaluations(
        left, right, target, publication=publication, control=control
    )
    first_ms = (time.perf_counter() - started) * 1000
    require(
        control.closure_confirmed and control.active_connections == 0,
        "comparison_connection_leak",
    )
    paths = {item.name: item.path for item in artifacts}
    reader = ComparisonReader(manifest, paths)
    event_id = reader.page("events", limit=1).items[0].event_pair_id
    manifest_file = next(
        item for item in artifacts if item.media_type == "application/json"
    )
    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        raw = manifest_file.path.read_bytes()
        require(
            hashlib.sha256(raw).hexdigest() == manifest_file.sha256,
            "comparison_manifest_integrity",
        )
        cached = ComparisonReader(ComparisonManifest.model_validate_json(raw), paths)
        page = cached.page("candidates", event_pair_id=event_id, limit=50)
        require(
            page.total == FROZEN_LISTINGS and len(page.items) == 50,
            "cached_comparison_rows",
        )
        samples.append((time.perf_counter() - started) * 1000)
    equivalence = full_comparison_audit(manifest, paths, left, right)
    return {
        "variant": variant,
        "physical_output_rows": equivalence["physical_output_rows"],
        "uncompressed_payload_bytes": equivalence["uncompressed_payload_bytes"],
        "expanded_payload_bytes": equivalence["expanded_payload_bytes"],
        "full_equivalence_audit": equivalence,
        "payload_size_boundary": "Actual stored compact JSON payload bytes, separately from exact expanded public ComparisonRecord bytes; compressed artifact bytes also include Parquet metadata and the manifest.",
        "comparison_deadline_seconds": 300,
        "first_comparison_ms": first_ms,
        "cached_comparison": timings(samples),
        "cached_boundary": "Hash-verified immutable comparison manifest read + new ComparisonReader + actual indexed candidate artifact page(50); not hash lookup alone, no recomputation, HTTP or PostgreSQL.",
        "sections": {
            key: value.model_dump(mode="json")
            for key, value in manifest.sections.items()
        },
        "observed_behavior": manifest.observed_behavior,
        "code_changed": manifest.code_changed,
        "manifest_sha256": manifest_file.sha256,
        "compressed_artifact_bytes": sum(item.size_bytes for item in artifacts),
        "artifact_count": len(artifacts),
        "output_rows": sum(item.rows for item in manifest.artifacts),
        "limits": manifest.limits.model_dump(mode="json"),
    }


def worker(args):
    work = args.work_dir.resolve()
    owned_temp = work / "temporary"
    owned_temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(owned_temp)
    started = time.perf_counter()
    result = {
        "status": "failed",
        "task": args.task,
        "variant": args.variant,
        "source_start": source_evidence(),
        "environment": environment_evidence(work),
    }
    try:
        operation = {
            "collect": lambda: collect(work, args.variant, args.sessions),
            "audit": lambda: audit(work, args.variant),
            "queries": lambda: queries(work, args.variant),
            "comparison": lambda: comparison(work, args.variant),
        }[args.task]
        result.update(operation())
        result["status"] = "passed"
    except Exception as error:  # noqa: BLE001 - preserve categorical failure evidence, never success
        trace = error.__traceback__
        while trace is not None:
            writer = trace.tb_frame.f_locals.get("writer")
            if writer is not None and type(writer).__name__ == "ComparisonWriter":
                result["failure_counters"] = {
                    name: getattr(writer, name)
                    for name in ("total_rows", "total_bytes", "published_bytes")
                }
                break
            trace = trace.tb_next
        result["failure"] = {
            "type": type(error).__name__,
            "code": getattr(
                error,
                "code",
                str(error) if isinstance(error, ValueError) else "operation_failed",
            ),
            "guard_message": getattr(error, "message", None),
        }
    result.update(
        {
            "elapsed_seconds": time.perf_counter() - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "pid": os.getpid(),
            "source_end": source_evidence(),
        }
    )
    write_json(args.task_report, result)
    return 0 if result["status"] == "passed" else 1


def validate_source(source):
    require(type(source) is dict and type(source["dirty"]) is bool, "invalid_source")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source["parent_head"]) is not None, "invalid_head"
    )
    files = source["relevant_files_sha256"]
    require(
        type(files) is dict
        and set(files) == set(source_evidence()["relevant_files_sha256"]),
        "missing_source_hashes",
    )
    require(
        all(
            type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            for value in files.values()
        ),
        "invalid_source_hashes",
    )
    require(source["relevant_tree_sha256"] == digest(files), "invalid_source_tree_hash")


def validate_report(report, *, require_clean=False):
    require(
        report["schema_version"] == REPORT_VERSION
        and report["fixture_version"] == FIXTURE_VERSION
    )
    require(report["status"] == "passed", "measurement_failed")
    require(
        report["profile"]
        == {
            "listings": FROZEN_LISTINGS,
            "sessions": FROZEN_SESSIONS,
            "variants": 3,
            "features_per_listing": 1,
            "predicates_per_listing": 1,
        },
        "nonrepresentative_profile",
    )
    validate_source(report["source_start"])
    validate_source(report["source_end"])
    require(
        report["source_start"]["parent_head"] == report["source_end"]["parent_head"],
        "head_changed_during_measurement",
    )
    require(
        report["source_start"]["relevant_files_sha256"]
        == report["source_end"]["relevant_files_sha256"],
        "source_changed_during_measurement",
    )
    require(report["environment"]["packages"]["duckdb"] == "1.4.5", "unpinned_duckdb")
    require(report["environment"]["reference"] == REFERENCE, "reference_changed")
    if require_clean:
        require(
            not report["source_start"]["dirty"] and not report["source_end"]["dirty"],
            "dirty_source",
        )
        require(
            report["source_start"]["parent_head"]
            == report["source_end"]["parent_head"]
            == command(["git", "rev-parse", "HEAD"]),
            "wrong_head",
        )
        require(
            report["source_end"]["relevant_files_sha256"]
            == source_evidence()["relevant_files_sha256"],
            "source_mismatch",
        )
    require(
        len(report["collection"])
        == len(report["audits"])
        == len(report["queries"])
        == 3
        and len(report["comparisons"]) == 2,
        "missing_measurements",
    )
    for index, item in enumerate(report["collection"]):
        require(
            item["variant"] == index
            and item["status"] == "passed"
            and item["coverage"] == "complete"
            and not item["missing_reasons"],
            "incomplete_capture",
        )
        require(
            item["decisions_seen"] == item["decisions_saved"] == FROZEN_SESSIONS
            and item["detail_rows_seen"]
            == item["detail_rows_saved"]
            == 3 * FROZEN_LISTINGS * FROZEN_SESSIONS,
            "truncated_capture",
        )
        require(
            item["table_rows"]
            == {
                table: FROZEN_SESSIONS
                * (
                    FROZEN_LISTINGS
                    if table in {"candidates", "features", "predicates"}
                    else 1
                )
                for table in TABLES
            },
            "truncated_tables",
        )
        require(
            item["elapsed_seconds"] > 0
            and item["peak_rss_bytes"] > 0
            and item["compressed_artifact_bytes"] > 0,
            "missing_collection_measurements",
        )
    for index, item in enumerate(report["audits"]):
        require(
            item["status"] == "passed"
            and item["variant"] == index
            and item["all_artifact_hashes_verified"] is True,
            "missing_physical_audit",
        )
        require(set(item["physical_tables"]) == set(TABLES), "missing_physical_tables")
        require(
            item["manifest_sha256"] == report["collection"][index]["manifest_sha256"],
            "audit_manifest_mismatch",
        )
        for table, counts in item["physical_tables"].items():
            require(
                counts["rows"] == report["collection"][index]["table_rows"][table]
                and counts["sessions"] == counts["decisions"] == FROZEN_SESSIONS,
                "physical_count_mismatch",
            )
            if table in {"candidates", "features", "predicates"}:
                require(
                    counts["exact_frozen_instrument_set"] is True,
                    "physical_universe_mismatch",
                )
                require(
                    all(
                        counts[key] == FROZEN_LISTINGS
                        for key in (
                            "min_rows_per_event",
                            "max_rows_per_event",
                            "min_instruments_per_event",
                            "max_instruments_per_event",
                        )
                    ),
                    "physical_pool_mismatch",
                )
    for index, item in enumerate(report["queries"]):
        require(
            item["variant"] == index and item["status"] == "passed", "query_failure"
        )
        require(
            set(item["queries"])
            == {
                f"{shape}.{mode}"
                for shape in ("candidate_page50", "instrument_stage_detail")
                for mode in ("first_open", "hot")
            },
            "missing_query_shapes",
        )
        for result in item["queries"].values():
            require(result == timings(result["samples_ms"]), "incorrect_percentiles")
            require(result["p95_ms"] <= 2000, "query_p95_exceeded")
    for variant, item in enumerate(report["comparisons"], 1):
        require(
            item["variant"] == variant
            and item["status"] == "passed"
            and item["code_changed"],
            "comparison_failed",
        )
        require(
            item["comparison_deadline_seconds"] == 300, "comparison_deadline_changed"
        )
        require(
            type(item["first_comparison_ms"]) in (int, float)
            and math.isfinite(item["first_comparison_ms"])
            and 0 < item["first_comparison_ms"] <= 300_000,
            "comparison_deadline_exceeded",
        )
        require(
            item["limits"] == DEFAULT_LIMITS.model_dump(mode="json"),
            "comparison_bounds_changed",
        )
        for section in ("candidates", "features", "predicates"):
            state = item["sections"][section]
            require(
                state["rows"] == FROZEN_LISTINGS * FROZEN_SESSIONS,
                "comparison_truncated",
            )
            require(
                state["changed"]
                == (
                    2 * FROZEN_SESSIONS
                    if variant == 2 and section == "candidates"
                    else 0
                ),
                "comparison_semantics",
            )
            require(
                state["left_availability"]
                == state["right_availability"]
                == "available",
                "comparison_coverage",
            )
        require(
            item["physical_output_rows"]
            == item["output_rows"]
            == sum(section["rows"] for section in item["sections"].values()),
            "comparison_output_audit",
        )
        proof = item["full_equivalence_audit"]
        require(
            proof["codec"] == PAYLOAD_CODEC
            and proof["payload_accounting_version"] == PAYLOAD_ACCOUNTING_VERSION
            and proof["all_rows_roundtripped"] is True
            and proof["all_source_observations_equivalent"] is True
            and proof["source_manifest_sha256"]
            == [
                report["collection"][index]["manifest_sha256"] for index in (0, variant)
            ]
            and proof["elapsed_seconds"] > 0,
            "comparison_full_equivalence_proof",
        )
        require(
            proof["deadline_seconds"] == AUDIT_DEADLINE_SECONDS
            and proof["elapsed_seconds"] <= AUDIT_DEADLINE_SECONDS
            and proof["closure_confirmed"] is True
            and 0
            < proof["source_scan_bytes"]
            <= proof["source_byte_budget"]
            == DEFAULT_LIMITS.max_input_bytes
            and proof["fixture_record_byte_budget"] == AUDIT_RECORD_BYTES,
            "comparison_audit_bounds",
        )
        require(
            set(proof["sections"]) == set(item["sections"]), "comparison_proof_sections"
        )
        for section, audited in proof["sections"].items():
            require(
                audited["rows"] == item["sections"][section]["rows"]
                and re.fullmatch(r"[0-9a-f]{64}", audited["expanded_canonical_sha256"])
                is not None
                and audited["source_observations_sha256"]
                == audited["restored_observations_sha256"]
                and len(audited["source_observations_sha256"]) == 2
                and all(
                    re.fullmatch(r"[0-9a-f]{64}", value) is not None
                    for value in audited["source_observations_sha256"]
                ),
                "comparison_proof_hashes",
            )
            if section in {"events", "targets", "candidates", "features", "predicates"}:
                expected = FROZEN_SESSIONS * (
                    FROZEN_LISTINGS
                    if section in {"candidates", "features", "predicates"}
                    else 2
                    if section == "targets"
                    else 1
                )
                require(
                    audited["side_observation_counts"] == [expected, expected]
                    and audited["event_pairs"] == FROZEN_SESSIONS,
                    "comparison_proof_source_coverage",
                )
        for field, section_field in (
            ("physical_output_rows", "rows"),
            ("uncompressed_payload_bytes", "stored_payload_bytes"),
            ("expanded_payload_bytes", "expanded_payload_bytes"),
        ):
            require(
                proof[field]
                == item[field]
                == sum(value[section_field] for value in proof["sections"].values()),
                "comparison_proof_totals",
            )
        require(
            item["expanded_payload_bytes"] >= item["uncompressed_payload_bytes"],
            "comparison_expanded_bytes",
        )
        require(
            0 < item["uncompressed_payload_bytes"] <= DEFAULT_LIMITS.max_output_bytes,
            "comparison_output_bytes",
        )
        require(
            item["sections"]["events"]["equal"] == FROZEN_SESSIONS,
            "event_pairing_semantics",
        )
        require(
            item["sections"]["targets"]["rows"]
            == FROZEN_SESSIONS * (2 if variant == 1 else 4),
            "target_row_count",
        )
        if variant == 1:
            require(
                item["sections"]["targets"]["observed_equality"] == "equal",
                "comment_only_target_change",
            )
            require(
                item["observed_behavior"] == "insufficient_coverage",
                "missing_economics_not_global_equality",
            )
        else:
            require(
                item["sections"]["targets"]["observed_equality"] == "different"
                and item["observed_behavior"] == "changed",
                "selected_rank_target_change",
            )
        require(
            item["sections"]["events"]["rows"] == FROZEN_SESSIONS, "event_pairing_count"
        )
        require(
            item["sections"]["orders"]["left_availability"] == "not_collected"
            and item["sections"]["turnover"]["observed_equality"] == "unknown",
            "invented_economics",
        )
        require(
            item["cached_comparison"]
            == timings(item["cached_comparison"]["samples_ms"]),
            "cached_measurement",
        )
        require(
            item["first_comparison_ms"] > 0
            and item["peak_rss_bytes"] > 0
            and item["compressed_artifact_bytes"] > 0,
            "missing_comparison_measurements",
        )
    for group in ("collection", "audits", "queries", "comparisons"):
        for item in report[group]:
            require(
                all(
                    type(item[field]) in (int, float)
                    and math.isfinite(item[field])
                    and item[field] > 0
                    for field in ("elapsed_seconds", "peak_rss_bytes")
                ),
                "missing_phase_measurement",
            )
            require(
                resume_identity(report["source_start"], item["environment"])[
                    "environment"
                ]
                == resume_identity(report["source_start"], report["environment"])[
                    "environment"
                ],
                "phase_environment_mismatch",
            )
            for source in (item["source_start"], item["source_end"]):
                validate_source(source)
                if require_clean:
                    require(not source["dirty"], "dirty_phase_source")
                require(
                    source["relevant_files_sha256"]
                    == report["source_start"]["relevant_files_sha256"]
                    and source["parent_head"] == report["source_start"]["parent_head"],
                    "phase_source_mismatch",
                )
    require(
        all(
            type(value) in (int, float) and math.isfinite(value) and value > 0
            for value in metric_values(report).values()
        ),
        "invalid_numeric_measurement",
    )
    return True


def metric_values(report):
    output = {}
    for name in ("collection", "comparisons"):
        for item in report[name]:
            for field in (
                "elapsed_seconds",
                "peak_rss_bytes",
                "compressed_artifact_bytes",
            ):
                output[f"{name}.{item['variant']}.{field}"] = item[field]
            if name == "comparisons":
                output[f"{name}.{item['variant']}.first_comparison_ms"] = item[
                    "first_comparison_ms"
                ]
                output[f"{name}.{item['variant']}.cached_p95_ms"] = item[
                    "cached_comparison"
                ]["p95_ms"]
    for item in report["queries"]:
        for name, result in item["queries"].items():
            output[f"queries.{item['variant']}.{name}.p95_ms"] = result["p95_ms"]
    return output


def freeze_budget(report, report_path):
    validate_report(report)
    measured = metric_values(report)
    return {
        "schema_version": BUDGET_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "baseline_report_sha256": sha(report_path),
        "baseline_source": report["source_end"],
        "baseline_environment": report["environment"],
        "policy": "First successful measured baseline; 2x elapsed/query time, 1.5x peak RSS/compressed bytes, with the frozen 2000ms query p95 target still mandatory. Engineering regression guardrail, not reference-hardware or production SLO certification.",
        "baseline_metrics": measured,
        "maximum_metrics": {
            key: value
            * (
                1.5
                if key.endswith(("peak_rss_bytes", "compressed_artifact_bytes"))
                else 2
            )
            for key, value in measured.items()
        },
    }


def validate_budget(report, budget, baseline):
    validate_report(report)
    validate_report(baseline)
    require(
        budget["schema_version"] == BUDGET_VERSION
        and budget["fixture_version"] == FIXTURE_VERSION,
        "invalid_budget",
    )
    require(budget["baseline_metrics"] == metric_values(baseline), "unmeasured_budget")
    require(
        budget["maximum_metrics"]
        == {
            key: value
            * (
                1.5
                if key.endswith(("peak_rss_bytes", "compressed_artifact_bytes"))
                else 2
            )
            for key, value in budget["baseline_metrics"].items()
        },
        "altered_budget",
    )
    require(
        set(metric_values(report)) == set(budget["maximum_metrics"]),
        "budget_metrics_missing",
    )
    for key, value in metric_values(report).items():
        require(value <= budget["maximum_metrics"][key], f"regression:{key}")


def resume_identity(source, environment):
    # Available disk changes as artifacts are written. Every other observed
    # runtime, hardware, quota and dependency field must remain exactly pinned.
    return {
        "head": source["parent_head"],
        "source_files": source["relevant_files_sha256"],
        "environment": {
            key: value
            for key, value in environment.items()
            if key != "disk_free_bytes_at_start"
        },
    }


def admit_resume(owner, source, environment):
    require(owner["fixture_version"] == FIXTURE_VERSION, "resume_fixture_changed")
    require(
        owner["resume_identity"] == resume_identity(source, environment),
        "resume_source_or_environment_changed",
    )


def admit_cached_phase(value, source, environment, *, require_clean=False):
    require(value["status"] == "passed", "cached_phase_failed")
    for label in ("source_start", "source_end"):
        original = value[label]
        validate_source(original)
        require(
            resume_identity(original, value["environment"])
            == resume_identity(source, environment),
            "cached_phase_provenance_changed",
        )
        if require_clean:
            require(not original["dirty"], "dirty_cached_phase")


def run(args):
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    marker = work / "benchmark-owner.json"
    current = source_evidence()
    environment = environment_evidence(work)
    if marker.exists():
        require(args.resume, "work_directory_already_used")
        admit_resume(read_json(marker), current, environment)
    else:
        require(not any(work.iterdir()), "work_directory_not_empty")
        write_json(
            marker,
            {
                "fixture_version": FIXTURE_VERSION,
                "source": current,
                "resume_identity": resume_identity(current, environment),
            },
        )
        write_json(work / "namespace.json", frozen_namespace().model_dump(mode="json"))
    report = {
        "schema_version": REPORT_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "status": "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "profile": {
            "listings": FROZEN_LISTINGS,
            "sessions": FROZEN_SESSIONS,
            "variants": 3,
            "features_per_listing": 1,
            "predicates_per_listing": 1,
        },
        "boundary": BOUNDARY,
        "source_start": current,
        "environment": environment,
        "collection": [],
        "audits": [],
        "queries": [],
        "comparisons": [],
    }
    write_json(work / f"run-start-{uuid4()}.json", report)
    started = time.perf_counter()
    for task, variants, key in (
        ("collect", range(3), "collection"),
        ("audit", range(3), "audits"),
        ("queries", range(3), "queries"),
        ("comparison", (1, 2), "comparisons"),
    ):
        for variant in variants:
            task_report = work / f"{task}-{variant}.json"
            if (
                task in {"collect", "audit"}
                and task_report.exists()
                and read_json(task_report)["status"] == "passed"
            ):
                value = read_json(task_report)
                admit_cached_phase(
                    value, current, environment, require_clean=args.require_clean
                )
                value["reused_immutable_phase"] = True
            else:
                if task_report.exists():
                    task_report = work / f"{task}-{variant}-{uuid4()}.json"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--task",
                        task,
                        "--variant",
                        str(variant),
                        "--sessions",
                        str(FROZEN_SESSIONS),
                        "--work-dir",
                        str(work),
                        "--task-report",
                        str(task_report),
                    ],
                    cwd=ROOT,
                    check=False,
                )
                value = (
                    read_json(task_report)
                    if task_report.exists()
                    else {
                        "status": "failed",
                        "variant": variant,
                        "failure": {
                            "code": "worker_failed",
                            "returncode": completed.returncode,
                        },
                    }
                )
            report[key].append(value)
            print(
                json.dumps(
                    {
                        "task": task,
                        "variant": variant,
                        "status": value["status"],
                        "elapsed_seconds": value.get("elapsed_seconds"),
                        "failure": value.get("failure"),
                    }
                ),
                flush=True,
            )
        if key in {"collection", "audits"} and any(
            value["status"] != "passed" for value in report[key]
        ):
            break
    report.update(
        {
            "source_end": source_evidence(),
            "elapsed_seconds": time.perf_counter() - started,
            "finished_at": datetime.now(UTC).isoformat(),
            "status": "passed",
        }
    )
    try:
        validate_report(report, require_clean=args.require_clean)
    except (ValueError, KeyError, TypeError) as error:
        report["status"] = "failed"
        report["validation_failure"] = str(error)
    write_json(args.report, report)
    if report["status"] == "passed" and args.freeze_budget:
        write_json(args.freeze_budget, freeze_budget(report, args.report))
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--freeze-budget", type=Path)
    parser.add_argument("--validate-report", type=Path)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--task",
        choices=("collect", "audit", "queries", "comparison"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--variant", type=int, choices=(0, 1, 2), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--sessions", type=int, default=FROZEN_SESSIONS, help=argparse.SUPPRESS
    )
    parser.add_argument("--task-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.task:
        return worker(args)
    if args.validate_report:
        report = read_json(args.validate_report)
        validate_report(report, require_clean=args.require_clean)
        if args.budget:
            require(args.baseline is not None, "baseline_required")
            budget = read_json(args.budget)
            require(
                sha(args.baseline) == budget["baseline_report_sha256"],
                "baseline_integrity",
            )
            validate_budget(report, budget, read_json(args.baseline))
        print("Representative trace capacity report validated.")
        return 0
    require(
        args.work_dir is not None and args.report is not None,
        "work_dir_and_report_required",
    )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
