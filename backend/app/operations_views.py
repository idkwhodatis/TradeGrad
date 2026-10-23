"""Read-only bounded operational samples; no scans of private files or queue args."""

import math
import os
import stat
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, text

from backend.app.models import (
    DataImport,
    ExecutionAttempt,
    ModelCall,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    utcnow,
)
from backend.app.operations_schemas import (
    DiskObservation,
    DurationObservation,
    ImportObservation,
    OperationsPublic,
    UsageObservation,
)

SAMPLE_LIMIT = 2000
QUEUE_LIMIT = 10000


def durations(values):
    valid = sorted(
        value
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )
    return DurationObservation(
        count=len(valid),
        unavailable_count=len(values) - len(valid),
        mean_seconds=sum(valid) / len(valid) if valid else None,
        p95_seconds=valid[math.ceil(len(valid) * 0.95) - 1] if valid else None,
        max_seconds=valid[-1] if valid else None,
    )


def seconds(start, end):
    if (
        start is None
        or end is None
        or start.utcoffset() is None
        or end.utcoffset() is None
    ):
        return None
    value = (end - start).total_seconds()
    return value if value >= 0 and math.isfinite(value) else None


def model_usage(rows, truncated=False):
    # Missing/invalid provider counts never become zero-cost claims. Charged tokens
    # are local reservation/budget accounting, not a provider invoice.
    known = invalid = 0
    totals = [0, 0, 0]
    charged_known = True
    for row in rows:
        bad = any(
            value is not None and (type(value) is not int or not 0 <= value <= 10**9)
            for value in row
        )
        invalid += int(bad)
        known += int(not bad and row[0] is not None and row[1] is not None)
        charged_known &= not bad and row[2] is not None
        if not bad:
            for index, value in enumerate(row):
                totals[index] += value or 0
    return UsageObservation(
        calls=len(rows),
        known_usage_calls=known,
        unknown_usage_calls=len(rows) - known,
        input_tokens=totals[0] if known == len(rows) else None,
        output_tokens=totals[1] if known == len(rows) else None,
        charged_tokens=totals[2] if charged_known else None,
        invalid_usage_calls=invalid,
        truncated=truncated,
    )


def disk_observation(storage_root, *, administrator):
    if not administrator:
        return DiskObservation(state="restricted")
    # Open existing directories component-by-component, without symlink traversal.
    # fstatvfs reads filesystem counters only, never files, names or backup paths.
    fd = None
    try:
        path = Path(os.path.abspath(storage_root))
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError
        info = os.fstatvfs(fd)
        values = [
            info.f_blocks * info.f_frsize,
            info.f_bfree * info.f_frsize,
            info.f_bavail * info.f_frsize,
        ]
        if (
            any(value < 0 for value in values)
            or values[1] > values[0]
            or values[2] > values[1]
        ):
            raise OSError
        return DiskObservation(
            state="observed",
            capacity_bytes=values[0],
            free_bytes=values[1],
            available_bytes=values[2],
        )
    except (OSError, ValueError, TypeError):
        return DiskObservation(state="unavailable")
    finally:
        if fd is not None:
            os.close(fd)


def read_operations(session, user, settings, *, scope, days):
    now = utcnow()
    start = now - timedelta(days=days)
    # Bounds database work even where time filtering cannot use an index. Nothing
    # is persisted and the session is rolled back by the request dependency.
    session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
    owner = scope == "owner"

    def scoped(statement, model):
        return statement.where(model.owner_id == user.id) if owner else statement

    run_rows = session.execute(
        scoped(
            select(ResearchRun.status, ResearchRun.created_at)
            .where(
                ResearchRun.status.in_(
                    (
                        "queued",
                        "preparing",
                        "running",
                        "finalizing",
                        "pause_requested",
                        "cancel_requested",
                    )
                )
            )
            .order_by(ResearchRun.created_at, ResearchRun.id)
            .limit(QUEUE_LIMIT + 1),
            ResearchRun,
        )
    ).all()
    import_rows = session.execute(
        scoped(
            select(DataImport.status, DataImport.created_at)
            .where(
                DataImport.status.in_(
                    ("requested", "fetching", "normalizing", "validating")
                )
            )
            .order_by(DataImport.created_at, DataImport.id)
            .limit(QUEUE_LIMIT + 1),
            DataImport,
        )
    ).all()
    queue_truncated = len(run_rows) > QUEUE_LIMIT or len(import_rows) > QUEUE_LIMIT
    queued_runs = [row[1] for row in run_rows[:QUEUE_LIMIT] if row[0] == "queued"]
    queued_imports = [
        row[1] for row in import_rows[:QUEUE_LIMIT] if row[0] == "requested"
    ]
    # Queued means domain work awaiting claim (including dispatched durable jobs),
    # NOT merely an undelivered outbox record or all Procrastinate job statuses.
    cancel_at = (
        select(RunEvent.occurred_at)
        .where(
            RunEvent.run_id == ExecutionAttempt.run_id,
            RunEvent.attempt_id == ExecutionAttempt.id,
            RunEvent.type == "stage_changed",
            RunEvent.payload["status"].as_string() == "cancel_requested",
        )
        .order_by(RunEvent.occurred_at, RunEvent.seq)
        .limit(1)
        .correlate(ExecutionAttempt)
        .scalar_subquery()
    )
    attempts = session.execute(
        scoped(
            select(
                ExecutionAttempt.started_at,
                ExecutionAttempt.finished_at,
                OutboxEvent.created_at,
                cancel_at,
                ExecutionAttempt.status,
                ExecutionAttempt.sandbox_stopped,
            )
            .join(ResearchRun, ResearchRun.id == ExecutionAttempt.run_id)
            .outerjoin(
                OutboxEvent,
                (OutboxEvent.run_id == ExecutionAttempt.run_id)
                & (OutboxEvent.generation == ExecutionAttempt.generation),
            )
            .where(
                ExecutionAttempt.started_at >= start, ExecutionAttempt.started_at <= now
            )
            .order_by(ExecutionAttempt.started_at.desc(), ExecutionAttempt.id)
            .limit(SAMPLE_LIMIT + 1),
            ResearchRun,
        )
    ).all()
    attempts_truncated = len(attempts) > SAMPLE_LIMIT
    attempts = attempts[:SAMPLE_LIMIT]
    imports = session.scalars(
        scoped(
            select(DataImport.status)
            .where(
                DataImport.status.in_(("ready", "failed", "partial", "cancelled")),
                DataImport.updated_at >= start,
                DataImport.updated_at <= now,
            )
            .order_by(DataImport.updated_at.desc(), DataImport.id)
            .limit(SAMPLE_LIMIT + 1),
            DataImport,
        )
    ).all()
    imports_truncated = len(imports) > SAMPLE_LIMIT
    imports = imports[:SAMPLE_LIMIT]
    counts = {
        status: imports.count(status)
        for status in ("ready", "failed", "partial", "cancelled")
    }
    denominator = counts["ready"] + counts["failed"] + counts["partial"]
    usage = session.execute(
        scoped(
            select(
                ModelCall.usage["input_tokens"],
                ModelCall.usage["output_tokens"],
                ModelCall.usage["charged_tokens"],
            )
            .join(ResearchRun, ResearchRun.id == ModelCall.run_id)
            .where(ModelCall.created_at >= start, ModelCall.created_at <= now)
            .order_by(ModelCall.created_at.desc(), ModelCall.id)
            .limit(SAMPLE_LIMIT + 1),
            ResearchRun,
        )
    ).all()
    return OperationsPublic(
        observed_at=now,
        window_start=start,
        scope=scope,
        sample_limit=SAMPLE_LIMIT,
        queued_runs=len(queued_runs),
        active_runs=min(len(run_rows), QUEUE_LIMIT) - len(queued_runs),
        active_imports=min(len(import_rows), QUEUE_LIMIT) - len(queued_imports),
        queued_imports=min(len(queued_imports), QUEUE_LIMIT),
        queue_counts_truncated=queue_truncated,
        current_queue_age=durations(
            [
                seconds(value, now)
                for value in queued_runs[:QUEUE_LIMIT] + queued_imports[:QUEUE_LIMIT]
            ]
        ),
        queue_wait=durations([seconds(row[2], row[0]) for row in attempts]),
        attempt_runtime=durations([seconds(row[0], row[1]) for row in attempts]),
        cancellation_latency=durations(
            [
                seconds(row[3], row[1])
                if row[4] == "cancelled" and row[5] is True
                else None
                for row in attempts
                if row[3] is not None
            ]
        ),
        attempts_truncated=attempts_truncated,
        imports=ImportObservation(
            sampled_completed=len(imports),
            **counts,
            failure_fraction=counts["failed"] / denominator if denominator else None,
            unsuccessful_fraction=(counts["failed"] + counts["partial"]) / denominator
            if denominator
            else None,
            truncated=imports_truncated,
        ),
        model_usage=model_usage(usage[:SAMPLE_LIMIT], len(usage) > SAMPLE_LIMIT),
        disk=disk_observation(settings.storage_root, administrator=user.is_superuser),
        notes=[
            "Recent bounded samples, not representative capacity or production performance guarantees. Truncation means incomplete population coverage.",
            "Queue counts cover domain queued runs and requested imports, including dispatched work. Active counts cover claimed/preparing/running/finalizing/cancel/pause-requested work; paused and interrupted work are excluded; queue age is since logical submission, not retry dispatch.",
            "Queue wait is attempt start minus matching generation outbox creation. Runtime is completed attempt wall time; unfinished or invalid durations are unavailable, not zero.",
            "Cancellation latency covers recorded cancel requests on attempts reaching cancelled; queued/paused immediate cancellation and unconfirmed stops are not inferred. It is not a process-tree containment proof.",
            "Import rates use latest terminal-state update in the window: failed / (ready + failed + partial); partial is separately shown. Cancelled is excluded from rate denominators.",
            "Model counters include attempted and unadopted calls. Charged tokens are local budget accounting, not currency cost. Unknown provider counts remain unknown.",
            "Disk is shared storage-filesystem capacity/free/available, not per-owner bytes or total application usage. No file inventory is exposed.",
        ],
    )
