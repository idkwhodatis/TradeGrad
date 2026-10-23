"""Transactional handoff: outbox and Procrastinate share a verified psycopg transaction.

Duplicate queue deliveries remain safe because claim_attempt locks the business
Run and checks its generation. We do not claim end-to-end exactly-once execution.
"""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.core import maintenance  # noqa: F401 -- before queue/row locks
from backend.app.models import OutboxEvent, utcnow
from workers.research.queue import TASK_NAME, create_queue_app


def dispatch_outbox(session: Session, limit: int = 100) -> int:
    if session.bind.dialect.name != "postgresql":
        raise ValueError("Outbox dispatcher requires PostgreSQL")
    events = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.dispatched_at.is_(None))
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not events:
        return 0
    connection = session.connection().connection.driver_connection
    url = session.bind.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    queue = create_queue_app(database_url=url)
    task = queue.tasks[TASK_NAME]
    for event in events:
        event.job_id = task.configure(connection=connection).defer(
            run_id=str(event.run_id), generation=event.generation
        )
        event.dispatched_at = utcnow()
    session.flush()
    return len(events)


def recover_stalled_jobs(session: Session, stale_seconds: int = 120) -> int:
    """Requeue only our jobs with missing/stale queue-worker heartbeats.

    Uses the pinned Procrastinate 3.10 heartbeat predicate and public retry SQL
    function under the caller's transaction. A redelivery still must claim the
    business Run: it cannot retry a live/stale attempt or skip confirmed stop.
    This also closes the crash window between queue delivery and business claim.
    """
    if stale_seconds < 30:
        raise ValueError("Queue stall threshold must be at least 30 seconds")
    jobs = (
        session.execute(
            text("""
        SELECT j.id FROM procrastinate_jobs j
        LEFT JOIN procrastinate_workers w ON w.id = j.worker_id
        WHERE j.task_name = :task AND j.queue_name = 'research'
          AND j.status = 'doing'
          AND (j.worker_id IS NULL OR w.last_heartbeat < now() - make_interval(secs => :seconds))
        ORDER BY j.id LIMIT 100 FOR UPDATE OF j SKIP LOCKED
    """),
            {"task": TASK_NAME, "seconds": stale_seconds},
        )
        .scalars()
        .all()
    )
    for job_id in jobs:
        session.execute(
            text("SELECT procrastinate_retry_job_v2(:job_id, now(), NULL, NULL, NULL)"),
            {"job_id": job_id},
        )
    return len(jobs)
