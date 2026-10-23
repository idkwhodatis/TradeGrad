"""Bounded trusted hosted helpers; no shell, credentials in argv or raw diagnostics."""

import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import contextmanager


class DeadlineExpired(TimeoutError):
    pass


@contextmanager
def alarm_deadline(deadline):
    """Absolute wall-clock cap, including DNS/headers/trickled response bytes.

    Hosted Linux main-thread only. No nested alarms: an existing alarm means
    ownership is uncertain and is rejected instead of extending another deadline.
    """
    remaining = deadline - time.monotonic()
    if sys.platform != "linux" or remaining <= 0:
        raise DeadlineExpired("Hosted request deadline unavailable or expired")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("Cannot replace an existing timer")
    previous = signal.getsignal(signal.SIGALRM)

    def expire(_signal, _frame):
        raise DeadlineExpired("Hosted request absolute deadline expired")

    signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def read_capped(stream, maximum):
    chunks = []
    size = 0
    while chunk := stream.read(min(65536, maximum + 1 - size)):
        size += len(chunk)
        if size > maximum:
            raise ValueError("Hosted response exceeds its byte budget")
        chunks.append(chunk)
    return b"".join(chunks)


def run_bounded(arguments, *, deadline, max_stdout=262144, max_stderr=65536):
    """Bound both pipe output and total wall time, including trickling CLI output.

    The trusted CLI starts in its own session. On failure its still-unreaped
    leader reserves the process-group identity for exact group teardown. Up to
    five seconds beyond the work deadline are reserved for kill/reap; uncertainty
    fails the caller. Detached untrusted execution never belongs in this helper.
    """
    if deadline <= time.monotonic():
        raise DeadlineExpired("Hosted command deadline expired before launch")
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    errors = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, kind in (
                (process.stdout, "stdout"),
                (process.stderr, "stderr"),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadlineExpired("Hosted command absolute deadline expired")
                for key, _ in selector.select(min(0.2, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "stdout":
                        if len(output) + len(chunk) > max_stdout:
                            raise ValueError("Hosted command output exceeded its bound")
                        output.extend(chunk)
                    else:
                        errors += len(chunk)
                        if errors > max_stderr:
                            raise ValueError(
                                "Hosted command diagnostics exceeded their bound"
                            )
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise RuntimeError("Hosted command failed or exceeded its deadline")
        return output.decode("utf-8", errors="strict")
    finally:
        try:
            if process.returncode is None:
                # Do not poll/reap before the group signal: the reserved leader
                # PID prevents group reuse while targeting this owned group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        finally:
            process.stdout.close()
            process.stderr.close()
