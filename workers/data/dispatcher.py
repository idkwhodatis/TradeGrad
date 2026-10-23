from sqlalchemy import select, text

from backend.app.core import maintenance  # noqa: F401 -- before queue/row locks
from backend.app.models import DataOutbox, utcnow
from workers.data.queue import TASK_NAME, create_data_queue


def dispatch_data_outbox(session, limit=100):
    if session.bind.dialect.name != "postgresql":
        raise ValueError("Data outbox requires PostgreSQL")
    rows = session.scalars(
        select(DataOutbox)
        .where(DataOutbox.dispatched_at.is_(None))
        .order_by(DataOutbox.created_at, DataOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not rows:
        return 0
    connection = session.connection().connection.driver_connection
    url = session.bind.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    queue = create_data_queue(database_url=url)
    for row in rows:
        row.job_id = (
            queue.tasks[TASK_NAME]
            .configure(connection=connection)
            .defer(import_id=str(row.import_id), generation=row.generation)
        )
        row.dispatched_at = utcnow()
    session.flush()
    return len(rows)


def recover_stalled_data_jobs(session, stale_seconds=120):
    if stale_seconds < 30:
        raise ValueError("Stall threshold must be at least 30 seconds")
    ids = (
        session.execute(
            text(
                """SELECT j.id FROM procrastinate_jobs j LEFT JOIN procrastinate_workers w ON w.id=j.worker_id WHERE j.task_name=:task AND j.queue_name='data' AND j.status='doing' AND (j.worker_id IS NULL OR w.last_heartbeat < now()-make_interval(secs=>:seconds)) ORDER BY j.id LIMIT 100 FOR UPDATE OF j SKIP LOCKED"""
            ),
            {"task": TASK_NAME, "seconds": stale_seconds},
        )
        .scalars()
        .all()
    )
    for job_id in ids:
        session.execute(
            text("SELECT procrastinate_retry_job_v2(:job_id,now(),NULL,NULL,NULL)"),
            {"job_id": job_id},
        )
    return len(ids)
