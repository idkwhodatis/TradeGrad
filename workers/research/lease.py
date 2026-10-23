"""Attempt-scoped fail-closed lease maintenance for bounded isolated jobs."""

import threading
import time

from sqlalchemy.dialects.postgresql import insert

from backend.app.models import WorkerHeartbeat, utcnow
from backend.app.services import heartbeat


class AttemptLease:
    def __init__(self, factory, settings, attempt_id, generation, token, worker_id):
        self.factory, self.settings = factory, settings
        self.attempt_id, self.generation, self.token = attempt_id, generation, token
        self.worker_id = worker_id
        self.cancelled, self.lost, self.done = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.lock = threading.Lock()
        self.last_pulse = 0.0
        self.thread = threading.Thread(target=self._maintain, daemon=True)

    def pulse(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_pulse < 1:
                return
            try:
                with self.factory.begin() as session:
                    if heartbeat(
                        session,
                        self.attempt_id,
                        self.generation,
                        self.token,
                        self.settings,
                    ):
                        self.cancelled.set()
                    session.execute(
                        insert(WorkerHeartbeat)
                        .values(
                            worker_id=self.worker_id,
                            role="research",
                            heartbeat_at=utcnow(),
                        )
                        .on_conflict_do_update(
                            index_elements=[WorkerHeartbeat.worker_id],
                            set_={"heartbeat_at": utcnow()},
                        )
                    )
                self.last_pulse = time.monotonic()
            except Exception:
                self.lost.set()
                self.cancelled.set()
                raise

    def _maintain(self):
        while not self.done.wait(
            min(self.settings.heartbeat_seconds, self.settings.lease_seconds / 3, 2)
        ):
            try:
                self.pulse(force=True)
            except Exception:  # noqa: BLE001 - lease failure revokes authority
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
