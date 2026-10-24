# ruff: noqa: SIM117
"""Actual local POSIX directory flock behavior, not PostgreSQL recovery proof."""

import os
import signal
import subprocess
import sys
from uuid import uuid4

import pytest

from backend.app.comparison_guard import (
    ComparisonGuardUnavailable,
    comparison_attempt_guard,
    comparison_query_confirmed_stopped,
)
from backend.app.core.maintenance import MaintenanceBusy
from backend.app.core.storage_barrier import storage_guard


def test_live_guard_blocks_probe_maintenance_and_duplicate_until_close(tmp_path):
    run, attempt = uuid4(), uuid4()
    with comparison_attempt_guard(tmp_path, run, attempt) as guard:
        assert guard.path.is_dir()
        guard.assert_owned(run, attempt)
        assert not comparison_query_confirmed_stopped(tmp_path, run, attempt)
        with pytest.raises(ComparisonGuardUnavailable):
            with comparison_attempt_guard(tmp_path, run, attempt):
                pytest.fail("Duplicate acquired live attempt")
        with pytest.raises(MaintenanceBusy):
            with storage_guard(tmp_path, exclusive=True):
                pytest.fail("Maintenance bypassed live root guard")
    assert comparison_query_confirmed_stopped(tmp_path, run, attempt)
    with pytest.raises(ComparisonGuardUnavailable):
        guard.assert_owned(run, attempt)


def test_probe_durably_provisions_missing_directory_without_changing_identity(tmp_path):
    run, attempt = uuid4(), uuid4()
    assert comparison_query_confirmed_stopped(tmp_path, run, attempt)
    target = tmp_path / "runs" / str(run) / "attempts" / str(attempt)
    before = target.stat()
    with comparison_attempt_guard(tmp_path, run, attempt):
        after = target.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


@pytest.mark.parametrize("component", ["runs", "attempts", "attempt"])
def test_symlink_or_unknown_directory_probe_fails_closed(tmp_path, component):
    run, attempt = uuid4(), uuid4()
    external = tmp_path / "external"
    external.mkdir()
    if component == "runs":
        path = tmp_path / "runs"
    elif component == "attempts":
        path = tmp_path / "runs" / str(run) / "attempts"
    else:
        path = tmp_path / "runs" / str(run) / "attempts" / str(attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(external, target_is_directory=True)
    assert not comparison_query_confirmed_stopped(tmp_path, run, attempt)


def test_sigkill_owned_guard_process_releases_kernel_lock_only_after_exit(tmp_path):
    run, attempt = uuid4(), uuid4()
    code = """
import sys, time
from backend.app.comparison_guard import comparison_attempt_guard
with comparison_attempt_guard(sys.argv[1], sys.argv[2], sys.argv[3]):
    print('held', flush=True)
    time.sleep(120)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(run), str(attempt)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        assert not comparison_query_confirmed_stopped(tmp_path, run, attempt)
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert comparison_query_confirmed_stopped(tmp_path, run, attempt)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()


def test_interrupt_and_permanent_close_failure_are_not_guard_release_proof(tmp_path):
    """Failure injection in an owned child, not a PostgreSQL/engine acceptance."""
    from select import select

    run, attempt = uuid4(), uuid4()
    code = """
import sys
from backend.app.comparison_guard import comparison_attempt_guard
from packages.research_trace.comparison_storage import ComparisonControl
class CannotConfirmClose:
    def execute(self, *args):
        return self
    def interrupt(self):
        pass
    def close(self):
        print('close-unconfirmed', flush=True)
        raise RuntimeError('synthetic permanent close failure')
import packages.research_trace.comparison_storage as storage
storage.duckdb.connect = lambda *a, **k: CannotConfirmClose()
with comparison_attempt_guard(sys.argv[1], sys.argv[2], sys.argv[3]):
    control = ComparisonControl(timeout_seconds=0.01)
    with control.connection() as connection:
        connection.execute('synthetic failure injection only')
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(run), str(attempt)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for _ in range(2):
            assert select([child.stdout], [], [], 10)[0]
            assert child.stdout.readline().strip() == "close-unconfirmed"
        assert child.poll() is None
        assert not comparison_query_confirmed_stopped(tmp_path, run, attempt)
        with pytest.raises(MaintenanceBusy):
            with storage_guard(tmp_path, exclusive=True):
                pytest.fail("Unconfirmed closure released root lifecycle")
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert comparison_query_confirmed_stopped(tmp_path, run, attempt)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
