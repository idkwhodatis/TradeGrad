"""Dedicated data runtime role: worker or transactional dispatcher/reconciler."""

import argparse
import logging
import os
import threading
import time
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from backend.app.core.config import get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.data_services import reconcile_imports
from backend.app.models import WorkerHeartbeat, utcnow
from workers.data.dispatcher import dispatch_data_outbox, recover_stalled_data_jobs
from workers.data.queue import create_data_queue

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["worker", "dispatcher"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    factory = session_factory(create_db_engine(settings))
    worker_id = f"data-{args.role}:{uuid4()}"

    def beat(session):
        session.execute(
            insert(WorkerHeartbeat)
            .values(
                worker_id=worker_id,
                role="data" if args.role == "worker" else "data_dispatcher",
                heartbeat_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_id],
                set_={"heartbeat_at": utcnow()},
            )
        )

    if args.role == "worker":
        done = threading.Event()

        def heartbeat():
            while not done.is_set():
                try:
                    with factory.begin() as session:
                        beat(session)
                except Exception:  # noqa: BLE001 - failure is fenced and safely classified, never logged with credentials
                    logger.error("Data worker heartbeat unavailable")
                done.wait(5)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            app = create_data_queue(settings)
            with app.open():
                app.run_worker(
                    queues=["data"],
                    concurrency=int(os.getenv("DATA_WORKER_CONCURRENCY", "1")),
                )
        finally:
            done.set()
            thread.join(timeout=15)
        return
    while True:
        try:
            with factory.begin() as session:
                beat(session)
                reconcile_imports(session, settings)
                dispatch_data_outbox(session)
                recover_stalled_data_jobs(session)
        except Exception:  # noqa: BLE001 - failure is fenced and safely classified, never logged with credentials
            logger.error("Data dispatcher cycle failed; database unavailable")
            if args.once:
                raise SystemExit(1) from None
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
