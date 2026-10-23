"""Hosted-only, bounded owner of the Playwright tree, including detached children.

This stdlib-only subprocess is a Linux subreaper. It never receives database or
model credentials. Node receives only the explicit generated E2E login material
and public local origins. All child output is discarded, not stored as evidence.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BROWSER_SECONDS = 300
GRACE_SECONDS = 15
CLEANUP_SECONDS = 10
ENV_KEYS = (
    "PATH",
    "HOME",
    "CI",
    "E2E_EMAIL",
    "E2E_PASSWORD",
    "E2E_OTHER_EMAIL",
    "E2E_OTHER_PASSWORD",
    "E2E_API_URL",
    "API_PROXY_TARGET",
    "E2E_MODEL_PROFILES",
    "E2E_MODEL_PROFILE_ID",
)


def browser_environment(source):
    """No arbitrary inherited settings, proxy hooks, Node options or model keys."""
    return {key: source[key] for key in ENV_KEYS if key in source}


def browser_command(node, root):
    return [
        str(node),
        str(Path(root) / "frontend/node_modules/playwright/cli.js"),
        "test",
        "tests/e2e/configured-research.spec.ts",
        "--grep",
        "@operator-model-profile",
        "--project=chromium",
        "--workers=1",
        "--retries=0",
        "--forbid-only",
        "--reporter=" + str(Path(root) / "tests/system/operator_profile_reporter.cjs"),
        f"--global-timeout={BROWSER_SECONDS * 1000}",
    ]


def contain(parent_pid):
    if (
        sys.platform != "linux"
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        raise RuntimeError("Unsupported hosted browser containment")
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER and PR_SET_PDEATHSIG. A dedicated supervisor adopts
    # detached Playwright/Chromium grandchildren; pytest's other children do not.
    if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise RuntimeError("Hosted browser containment unavailable")
    if os.getppid() != parent_pid:
        raise RuntimeError("Hosted browser parent changed before launch")


def direct_children():
    raw = Path(f"/proc/self/task/{os.getpid()}/children").read_text()
    children = [int(value) for value in raw.split()]
    if len(children) > 1024:
        raise RuntimeError("Hosted browser child bound exceeded")
    return children


def stop_direct_children():
    """Signal exact adopted children only; pidfd prevents PID-reuse targeting."""
    for pid in direct_children():
        fd = None
        try:
            fd = os.pidfd_open(pid)
            # Recheck ownership after opening the identity-bound handle. Children
            # are not concurrently reaped by this single-threaded supervisor.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != os.getpid():
                raise RuntimeError("Hosted browser child ownership changed")
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            if fd is not None:
                os.close(fd)


def reap_children(leader):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if not pid:
            return False
        if leader is not None and pid == leader.pid:
            leader.returncode = os.waitstatus_to_exitcode(status)


def supervise(node, root, parent_pid):
    interrupted = False

    def interrupt(_signal, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    contain(parent_pid)
    leader = None
    leader_fd = None
    failed = False
    stopped = False
    try:
        leader = subprocess.Popen(
            browser_command(node, root),
            cwd=Path(root) / "frontend",
            env=browser_environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        leader_fd = os.pidfd_open(leader.pid)
        # Allow Playwright's own global timeout to execute its normal teardown.
        deadline = time.monotonic() + BROWSER_SECONDS + 15
        while (
            leader.returncode is None
            and not interrupted
            and time.monotonic() < deadline
        ):
            reap_children(leader)
            time.sleep(0.1)
        if leader.returncode is None:
            failed = True
            try:
                signal.pidfd_send_signal(leader_fd, signal.SIGINT)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + GRACE_SECONDS
            while leader.returncode is None and time.monotonic() < deadline:
                reap_children(leader)
                time.sleep(0.1)
        failed = failed or interrupted or leader.returncode != 0
    except BaseException:  # noqa: BLE001 - uncertain launch must still be contained
        # Even an uncertain launch must run child discovery/cleanup.
        failed = True
    finally:
        # Killing a root adopts its detached descendants here. Repeated exact
        # child discovery/reaping reaches the entire owned tree, not other jobs.
        deadline = time.monotonic() + CLEANUP_SECONDS
        while time.monotonic() < deadline:
            try:
                stop_direct_children()
                if reap_children(leader):  # ECHILD, not an empty /proc snapshot.
                    stopped = True
                    break
            except Exception:  # noqa: BLE001 - cleanup must retry without private diagnostics
                failed = True
            time.sleep(0.05)
        if leader_fd is not None:
            os.close(leader_fd)
    return 0 if stopped and not failed and not interrupted else 1


def main():
    # These control flags are stripped before spawning Node.
    if not (
        os.getenv("GITHUB_ACTIONS") == "true"
        and os.getenv("TRADEGRAD_REQUIRE_MODEL_INTEGRATION") == "1"
        and os.getenv("TRADEGRAD_REQUIRE_MODEL_BROWSER") == "1"
    ):
        return 1
    if len(sys.argv) != 4:
        return 1
    try:
        return supervise(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]))
    except BaseException:  # noqa: BLE001 - no private child diagnostics
        # Never emit E2E credentials, inherited values or subprocess tracebacks.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
