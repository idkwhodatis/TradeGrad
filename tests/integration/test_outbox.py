"""Use the installed Procrastinate schema and real transactional queue inserts."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres


def test_outbox_dispatch_persists_one_procrastinate_job_and_is_replay_safe(
    migrated_engine, queued_run
):
    from backend.app.models import OutboxEvent
    from workers.research.dispatcher import dispatch_outbox

    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 0
        session.commit()
        event = session.scalar(select(OutboxEvent))
        assert event.dispatched_at is not None
        jobs = (
            session.execute(text("SELECT id, args, status FROM procrastinate_jobs"))
            .mappings()
            .all()
        )
        assert len(jobs) == 1
        assert event.job_id == jobs[0]["id"]
        assert jobs[0]["args"]["run_id"] == queued_run["id"]
        assert jobs[0]["args"]["generation"] == 1
        assert jobs[0]["status"] == "todo"


def test_queue_insert_and_outbox_acknowledgement_roll_back_together(
    migrated_engine, queued_run
):
    from backend.app.models import OutboxEvent
    from workers.research.dispatcher import dispatch_outbox

    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 1
        # Simulates failure after deferral and before committing the dispatch unit.
        session.rollback()
    with Session(migrated_engine) as session:
        event = session.scalar(select(OutboxEvent))
        assert event.dispatched_at is None
        assert event.job_id is None
        assert session.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 0
        assert dispatch_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 1
        assert session.scalar(select(OutboxEvent)).dispatched_at is not None


def test_concurrent_dispatchers_do_not_enqueue_twice(migrated_engine, queued_run):
    from backend.app.models import OutboxEvent
    from workers.research.dispatcher import dispatch_outbox

    barrier = Barrier(2)

    def dispatch(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            count = dispatch_outbox(session)
            session.commit()
            return count

    with ThreadPoolExecutor(max_workers=2) as executor:
        counts = list(executor.map(dispatch, range(2)))
    assert sorted(counts) == [0, 1]
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 1
        assert session.scalar(select(OutboxEvent)).job_id is not None


def test_cancelled_old_delivery_does_not_block_or_claim_resumed_generation(
    migrated_engine, settings, owner, queued_run
):
    from backend.app.services import claim_attempt, request_cancel, resume_run
    from workers.research.dispatcher import dispatch_outbox

    run_id = UUID(queued_run["id"])
    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        request_cancel(session, run_id, owner.user.id)
        assert resume_run(session, run_id, owner.user.id).generation == 2
        session.commit()
    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert claim_attempt(session, run_id, 1, "old-queue-message", settings) is None
        current = claim_attempt(session, run_id, 2, "new-queue-message", settings)
        assert current is not None
        assert current.generation == 2
        session.commit()
    with Session(migrated_engine) as session:
        jobs = (
            session.execute(text("SELECT args FROM procrastinate_jobs ORDER BY id"))
            .mappings()
            .all()
        )
        assert [job["args"]["generation"] for job in jobs] == [1, 2]


@pytest.fixture
def stale_queue_delivery(migrated_engine, queued_run):
    """Fetch through Procrastinate's real SQL, then simulate a dead queue worker."""
    from workers.research.dispatcher import dispatch_outbox

    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) == 1
        worker_id = session.scalar(
            text("SELECT worker_id FROM procrastinate_register_worker_v1()")
        )
        job_id = session.scalar(
            text(
                "SELECT (procrastinate_fetch_job_v2(ARRAY['research']::varchar[], :worker)).id"
            ),
            {"worker": worker_id},
        )
        assert job_id is not None
        session.execute(
            text(
                "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes' WHERE id = :worker"
            ),
            {"worker": worker_id},
        )
        session.commit()
    return job_id, worker_id


def test_crash_before_business_claim_is_redelivered_once(
    migrated_engine, settings, queued_run, stale_queue_delivery
):
    from backend.app.models import ExecutionAttempt, ResearchRun
    from backend.app.services import claim_attempt
    from workers.research.dispatcher import recover_stalled_jobs

    job_id, _ = stale_queue_delivery
    with Session(migrated_engine) as session:
        assert session.get(ResearchRun, UUID(queued_run["id"])).status == "queued"
        assert recover_stalled_jobs(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert recover_stalled_jobs(session) == 0
        row = session.execute(
            text("SELECT status, attempts FROM procrastinate_jobs WHERE id = :id"),
            {"id": job_id},
        ).one()
        assert tuple(row) == ("todo", 1)
        worker = session.scalar(
            text("SELECT worker_id FROM procrastinate_register_worker_v1()")
        )
        fetched = session.scalar(
            text(
                "SELECT (procrastinate_fetch_job_v2(ARRAY['research']::varchar[], :worker)).id"
            ),
            {"worker": worker},
        )
        assert fetched == job_id
        attempt = claim_attempt(
            session, UUID(queued_run["id"]), 1, "replacement", settings
        )
        assert attempt is not None
        session.commit()
    with Session(migrated_engine) as session:
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1


def test_recovery_includes_deleted_queue_worker(migrated_engine, stale_queue_delivery):
    from workers.research.dispatcher import recover_stalled_jobs

    job_id, worker = stale_queue_delivery
    with Session(migrated_engine) as session:
        session.execute(
            text("SELECT procrastinate_unregister_worker_v1(:worker)"),
            {"worker": worker},
        )
        session.commit()
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                text("SELECT worker_id FROM procrastinate_jobs WHERE id = :id"),
                {"id": job_id},
            )
            is None
        )
        assert recover_stalled_jobs(session) == 1
        session.commit()


@pytest.mark.parametrize(
    "exclude", ["healthy-worker", "other-task", "other-queue", "finished-job"]
)
def test_recovery_does_not_touch_unrelated_or_healthy_jobs(
    migrated_engine, stale_queue_delivery, exclude
):
    from workers.research.dispatcher import recover_stalled_jobs

    job_id, worker = stale_queue_delivery
    with Session(migrated_engine) as session:
        if exclude == "healthy-worker":
            session.execute(
                text("SELECT procrastinate_update_heartbeat_v1(:worker)"),
                {"worker": worker},
            )
        elif exclude == "other-task":
            session.execute(
                text(
                    "UPDATE procrastinate_jobs SET task_name = 'unrelated.task' WHERE id = :id"
                ),
                {"id": job_id},
            )
        elif exclude == "other-queue":
            session.execute(
                text(
                    "UPDATE procrastinate_jobs SET queue_name = 'unrelated' WHERE id = :id"
                ),
                {"id": job_id},
            )
        else:
            session.execute(
                text("SELECT procrastinate_finish_job_v1(:id, 'succeeded', false)"),
                {"id": job_id},
            )
        session.commit()
    with Session(migrated_engine) as session:
        assert recover_stalled_jobs(session) == 0
        assert session.scalar(
            text("SELECT status FROM procrastinate_jobs WHERE id = :id"), {"id": job_id}
        ) == ("succeeded" if exclude == "finished-job" else "doing")


def test_concurrent_stalled_job_recovery_retries_only_once(
    migrated_engine, stale_queue_delivery
):
    from workers.research.dispatcher import recover_stalled_jobs

    barrier = Barrier(2)

    def recover(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            count = recover_stalled_jobs(session)
            session.commit()
            return count

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(recover, range(2))) == [0, 1]
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT attempts FROM procrastinate_jobs")) == 1


def test_stalled_job_recovery_rolls_back_and_preserves_business_fence(
    migrated_engine, settings, claimed_attempt, stale_queue_delivery
):
    from backend.app.models import ExecutionAttempt, ResearchRun
    from backend.app.services import claim_attempt
    from workers.research.dispatcher import recover_stalled_jobs

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        assert recover_stalled_jobs(session) == 1
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT status FROM procrastinate_jobs")) == "doing"
        assert session.scalar(text("SELECT attempts FROM procrastinate_jobs")) == 0
        assert recover_stalled_jobs(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert claim_attempt(session, attempt.run_id, 1, "redelivery", settings) is None
        assert session.get(ResearchRun, attempt.run_id).active_attempt_id == attempt.id
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1
