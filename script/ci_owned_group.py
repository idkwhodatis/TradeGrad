"""Identity-safe process-group ownership for single-threaded hosted test harnesses.

Admit immediately after Popen(start_new_session=True). Pin an exact pidfd and
never reap until the leader is exited and its session has no live members.
WNOWAIT reserves the leader PID across group signals, preventing PGID reuse.
Already-reaped/unadmitted identities are NOT cleanup proof and are never signalled.
No concurrent wait/poll/reaper may operate on admitted Popen objects.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

MAX_PROC_ENTRIES = 8192


def process_fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def admit_owned_group(process):
    if getattr(process, "_tradegrad_group", None) is not None:
        raise RuntimeError("Process group was already admitted")
    if process.returncode is not None:
        raise RuntimeError("Cannot admit an already-reaped process group")
    fd = os.pidfd_open(process.pid)
    try:
        fields = process_fields(process.pid)
        if (
            int(fields[1]) != os.getpid()
            or int(fields[2]) != process.pid
            or int(fields[3]) != process.pid
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        ):
            raise RuntimeError("Exclusive direct-child session ownership unavailable")
        # WNOWAIT also rejects handles whose original child was already reaped.
        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        process._tradegrad_group = {"fd": fd, "start": fields[19], "verified": False}
    except BaseException:
        os.close(fd)
        raise


def identity(process):
    value = getattr(process, "_tradegrad_group", None)
    if value is None or value["verified"] or process.returncode is not None:
        raise RuntimeError("Owned group identity has been reaped or is unconfirmed")
    state = os.waitid(os.P_PIDFD, value["fd"], os.WEXITED | os.WNOHANG | os.WNOWAIT)
    fields = process_fields(process.pid)
    if (
        int(fields[1]) != os.getpid()
        or int(fields[2]) != process.pid
        or int(fields[3]) != process.pid
        or fields[19] != value["start"]
    ):
        raise RuntimeError("Owned group identity changed")
    return value, state


def peek_owned_group(process):
    value = getattr(process, "_tradegrad_group", None)
    if value is not None and value["verified"]:
        return process.returncode
    _, state = identity(process)
    if state is None:
        return None
    if state.si_code == os.CLD_EXITED:
        return state.si_status
    if state.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -state.si_status
    raise RuntimeError("Unexpected owned-child wait status")


def signal_owned_group(process, signum):
    identity(process)
    # No poll/wait/reap between the direct-child WNOWAIT proof and killpg.
    # The unreaped session leader reserves this numeric process-group identity.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def signal_owned_process(process, signum):
    value, _ = identity(process)
    try:
        signal.pidfd_send_signal(value["fd"], signum)
    except ProcessLookupError:
        pass


def session_quiescent(process):
    # Read-only discovery never authorizes signalling another PID or group.
    # The leader stays unreaped for the full observation, so its SID cannot be
    # recycled into another session. Detached same-session groups fail closed.
    identity(process)
    entries = list(Path("/proc").iterdir())
    if len(entries) > MAX_PROC_ENTRIES:
        raise RuntimeError("Process inventory exceeded hosted cleanup bound")
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            fields = process_fields(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[3]) == process.pid and fields[0] not in {"Z", "X"}:
            return False
    return True


def finish_owned_group(process):
    value = getattr(process, "_tradegrad_group", None)
    if value is not None and value["verified"]:
        return True
    if peek_owned_group(process) is None or not session_quiescent(process):
        return False
    # A second complete observation excludes a transient disappearing member.
    if not session_quiescent(process):
        return False
    process.wait(timeout=0)
    value["verified"] = True
    os.close(value["fd"])
    value["fd"] = None
    return True


def wait_owned_group(process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if finish_owned_group(process):
            return process.returncode
        time.sleep(0.05)
    raise TimeoutError("Owned process/session stop is not confirmed")


def stop_owned_groups(processes, *, initial_signal=signal.SIGTERM):
    """Batch stop with a single 15-second budget, not N independent timeouts."""
    pending = list(processes)
    failed = False
    for signum, seconds in ((initial_signal, 5), (signal.SIGKILL, 10)):
        for process in pending:
            try:
                value = getattr(process, "_tradegrad_group", None)
                if not (value is not None and value["verified"]):
                    signal_owned_group(process, signum)
            except (OSError, RuntimeError, ValueError, IndexError):
                failed = True
        deadline = time.monotonic() + seconds
        while pending and time.monotonic() < deadline:
            remaining = []
            for process in pending:
                try:
                    if not finish_owned_group(process):
                        remaining.append(process)
                except (OSError, RuntimeError, ValueError, IndexError):
                    failed = True
                    remaining.append(process)
            pending = remaining
            if pending:
                time.sleep(0.05)
        if not pending:
            break
    if pending or failed:
        raise RuntimeError("Owned process/session cleanup is unconfirmed")
