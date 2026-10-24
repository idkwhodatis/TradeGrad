"""Live in-process comparison exclusion, independent of PostgreSQL or Docker.

The attempt directory is the lock inode. It is durably provisioned with no-follow
opens and must never be renamed/deleted by retention. A lease timeout, interrupt
call, or Docker stop is not proof that a synchronous DuckDB query has stopped.
"""

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from backend.app.core.atomic_storage import (
    _absolute_path,
    _open_directory,
    _prepare_directory,
)
from backend.app.core.storage_barrier import storage_guard


class ComparisonGuardUnavailable(RuntimeError):
    """Live, unknown, or unsafe directory identity: recovery must fail closed."""


@dataclass
class ComparisonAttemptGuard:
    run_id: UUID
    attempt_id: UUID
    path: Path
    descriptor: int
    active: bool = True

    def assert_owned(self, run_id, attempt_id):
        if not self.active or self.run_id != run_id or self.attempt_id != attempt_id:
            raise ComparisonGuardUnavailable("Comparison attempt guard is not held")
        current = _open_directory(self.path)
        try:
            expected, actual = os.fstat(self.descriptor), os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise ComparisonGuardUnavailable("Comparison guard directory changed")
        finally:
            os.close(current)


@contextmanager
def comparison_attempt_guard(storage_root, run_id, attempt_id):
    """Acquire nonblocking exclusive flock; retain it through actual query close.

    Workers must first commit their claim, acquire this guard, then perform a
    fresh fenced_attempt/mark_running transaction BEFORE source reads or SQL.
    Reconcilers probe under their existing Run -> Attempt row locks. They may
    release before commit: a delayed worker's fresh fence waits for those locks
    and loses after reconciliation rotates the token.
    """
    root = _absolute_path(Path(storage_root))
    run_id, attempt_id = UUID(str(run_id)), UUID(str(attempt_id))
    path = root / "runs" / str(run_id) / "attempts" / str(attempt_id)
    fd = None
    guard = None
    try:
        with storage_guard(root):
            fd = _prepare_directory(path, root)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ComparisonGuardUnavailable(
                    "Comparison query remains live"
                ) from None
            guard = ComparisonAttemptGuard(run_id, attempt_id, path, fd)
            guard.assert_owned(run_id, attempt_id)
            try:
                yield guard
            finally:
                guard.active = False
                os.close(fd)
                fd = None
    except ComparisonGuardUnavailable:
        raise
    except (OSError, ValueError) as error:
        raise ComparisonGuardUnavailable(
            "Comparison guard identity is uncertain"
        ) from error
    finally:
        if guard is not None:
            guard.active = False
        if fd is not None:
            os.close(fd)


def comparison_query_confirmed_stopped(storage_root, run_id, attempt_id):
    """No Docker fallback. Missing directories are provisioned before probing."""
    try:
        with comparison_attempt_guard(storage_root, run_id, attempt_id):
            return True
    except Exception:  # noqa: BLE001 -- every unknown probe result is nontermination
        return False
