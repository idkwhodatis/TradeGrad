"""Run one role per process: worker, dispatcher or reconciler."""

import argparse
import logging
import os
import socket
import threading
import time
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.app.core.config import get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.models import ExecutionAttempt, WorkerHeartbeat, utcnow
from backend.app.services import reconcile_attempt
from workers.research.dispatcher import dispatch_outbox, recover_stalled_jobs
from workers.research.queue import create_queue_app

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["worker", "dispatcher", "reconciler"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    factory = session_factory(create_db_engine(settings))
    worker_id = f"{args.role}:{socket.gethostname()}:{uuid4()}"
    if args.role == "worker":
        done = threading.Event()

        def keep_alive():
            while not done.is_set():
                try:
                    with factory.begin() as session:
                        session.execute(
                            insert(WorkerHeartbeat)
                            .values(
                                worker_id=worker_id,
                                role="research",
                                heartbeat_at=utcnow(),
                            )
                            .on_conflict_do_update(
                                index_elements=[WorkerHeartbeat.worker_id],
                                set_={"heartbeat_at": utcnow()},
                            )
                        )
                except Exception:  # noqa: BLE001 - heartbeat reports stale rather than crashing worker
                    logger.error("Research worker heartbeat unavailable")
                done.wait(5)

        thread = threading.Thread(target=keep_alive, daemon=True)
        thread.start()
        app = create_queue_app(settings)
        try:
            with app.open():
                app.run_worker(
                    queues=["research"],
                    concurrency=int(os.getenv("WORKER_CONCURRENCY", "1")),
                )
        finally:
            done.set()
            thread.join(timeout=20)
        return
    runner = portable = None
    if args.role == "reconciler":
        from runtimes.evaluator.portable_runner import PortableDockerEvaluator
        from runtimes.evaluator.runner import DockerEvaluator
        from workers.research.containment import AttemptSandboxStopper

        runner = DockerEvaluator()
        portable = PortableDockerEvaluator()
    while True:
        try:
            with factory.begin() as session:
                session.execute(
                    insert(WorkerHeartbeat)
                    .values(worker_id=worker_id, role=args.role, heartbeat_at=utcnow())
                    .on_conflict_do_update(
                        index_elements=[WorkerHeartbeat.worker_id],
                        set_={"heartbeat_at": utcnow()},
                    )
                )
                if args.role == "dispatcher":
                    dispatch_outbox(session)
                else:
                    ids = session.scalars(
                        select(ExecutionAttempt.id)
                        .where(
                            ExecutionAttempt.finished_at.is_(None),
                            ExecutionAttempt.lease_expires_at <= utcnow(),
                        )
                        .limit(100)
                    ).all()
                    for attempt_id in ids:
                        stopper = AttemptSandboxStopper(attempt_id, runner, portable)
                        reconcile_attempt(session, attempt_id, stopper.stop)
                    recover_stalled_jobs(session)
        except Exception:  # noqa: BLE001 - service loop must remain available after transient outages
            # Do not serialize SQLAlchemy exceptions/connection strings into logs.
            logger.error(
                "%s cycle failed; database or sandbox may be unavailable", args.role
            )
            if args.once:
                raise SystemExit(1) from None
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
