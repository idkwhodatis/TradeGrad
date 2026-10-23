"""Single-threaded Linux owner for disposable hosted application subprocesses.

No shell, numeric-PID signal or process-group broadcast is used. Popen objects
stay strongly referenced and only this owner reaps them. pidfds pin exact kernel
identities; adopted detached descendants are discovered by the reviewed Compose
subreaper cleanup. ECHILD, not an empty /proc snapshot, proves final absence.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from script.compose_browser_launcher import (
    contain,
    direct_children,
    stop_direct_children,
)

GRACE_SECONDS = 5
CLEANUP_SECONDS = 15
MAX_ROOTS = 1024


class OwnedProcess:
    def __init__(self, process, service):
        self.process = process
        self.service = service
        self.fd = None


class ProcessOwner:
    def __init__(self, deadline):
        self.deadline = deadline
        self.children = {}
        self.interrupted = False
        self.started = False

    def contain(self):
        parent = os.getppid()

        def interrupt(_signal, _frame):
            self.interrupted = True

        # Never replace another child-reaping policy or adopt existing children.
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL or direct_children():
            raise RuntimeError("Exclusive child ownership unavailable")
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        contain(parent)
        self.started = True

    def spawn(self, arguments, *, cwd, env, service=False, capture=False):
        if not self.started or len(self.children) >= MAX_ROOTS:
            raise RuntimeError("Hosted child ownership unavailable or exhausted")
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        child = OwnedProcess(process, service)
        # Register BEFORE pidfd_open: even failure to pin must clean this child.
        self.children[process.pid] = child
        child.fd = os.pidfd_open(process.pid)
        return child

    def reap(self):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if not pid:
                return False
            child = self.children.get(pid)
            if child is not None and child.process.returncode is None:
                child.process.returncode = os.waitstatus_to_exitcode(status)
                if child.fd is not None:
                    os.close(child.fd)
                    child.fd = None

    def signal_child(self, child, signum):
        # A reaped root has released its PID. Never consult or signal that PID.
        if child.process.returncode is not None:
            return
        if child.fd is None:
            raise RuntimeError("Owned process identity is not pinned")
        fields = (
            Path(f"/proc/{child.process.pid}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()
        )
        if int(fields[1]) != os.getpid():
            raise RuntimeError("Owned process parent changed")
        try:
            signal.pidfd_send_signal(child.fd, signum)
        except ProcessLookupError:
            pass

    def check(self):
        self.reap()
        if self.interrupted or time.monotonic() >= self.deadline:
            raise RuntimeError("Hosted application interrupted or expired")
        if any(
            child.service and child.process.returncode is not None
            for child in self.children.values()
        ):
            raise RuntimeError("Required hosted service exited")

    def run(self, arguments, *, cwd, env, seconds, maximum=0, cleanup=False):
        """Bound wall time and captured bytes; all diagnostics are discarded."""
        deadline = time.monotonic() + seconds
        if not cleanup:
            self.check()
            deadline = min(deadline, self.deadline)
        child = self.spawn(arguments, cwd=cwd, env=env, capture=bool(maximum))
        output = bytearray()
        stream = child.process.stdout if maximum else None
        try:
            with selectors.DefaultSelector() as selector:
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while True:
                    if cleanup:
                        self.reap()
                    else:
                        self.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned command exceeded its deadline")
                    if child.process.returncode is not None and not selector.get_map():
                        if child.process.returncode != 0:
                            raise RuntimeError("Owned command failed")
                        return bytes(output)
                    if selector.get_map():
                        for key, _ in selector.select(0.1):
                            chunk = os.read(
                                key.fileobj.fileno(),
                                min(65536, maximum + 1 - len(output)),
                            )
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                output.extend(chunk)
                                if len(output) > maximum:
                                    raise ValueError(
                                        "Owned command output exceeded its bound"
                                    )
                    else:
                        time.sleep(0.1)
        finally:
            if stream is not None:
                stream.close()
            # On error, DO NOT poll, wait or signal a saved numeric PID. The
            # caller's mandatory final cleanup contains this entire tree.

    def cleanup(self):
        if not self.started:
            return False
        failed = False
        try:
            for child in self.children.values():
                self.signal_child(child, signal.SIGTERM)
        except Exception:  # noqa: BLE001 - uncertain cleanup is failure, never a private traceback
            failed = True
        deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < deadline:
            if self.reap():
                return not failed
            time.sleep(0.05)
        deadline = time.monotonic() + CLEANUP_SECONDS
        while time.monotonic() < deadline:
            try:
                # Reviewed pidfd+parent recheck handles both original children
                # and newly adopted descendants, including detached Chromium.
                stop_direct_children()
                if self.reap():
                    return not failed
            except Exception:  # noqa: BLE001 - uncertain cleanup is failure, never a private traceback
                failed = True
            time.sleep(0.05)
        return False
