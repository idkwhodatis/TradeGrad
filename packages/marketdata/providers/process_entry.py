"""Linux data-runtime process limits before importing any provider SDK."""

import ctypes
import os
import resource
import runpy
import signal
import sys
import threading
import time

ALLOWED = {
    "packages.marketdata.providers.sdk_worker",
    "packages.marketdata.providers.openbb_sdk",
}


def main():
    module, output, maximum, expected_parent = sys.argv[1:]
    parent = int(expected_parent)
    owned_group = os.getpid()
    if parent <= 1 or os.getpgrp() != owned_group:
        raise SystemExit(2)
    if module not in ALLOWED:
        raise SystemExit(2)
    maximum = int(maximum)
    if not 1024 <= maximum <= 100_000_000:
        raise SystemExit(2)

    def stop_group(*_):
        os.killpg(owned_group, signal.SIGKILL)

    # The creator PID is fixed before Popen, not discovered after Python startup.
    # A worker killed before this entry point runs must never be replaced by PID1.
    if os.getppid() != parent:
        stop_group()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    signal.signal(signal.SIGTERM, stop_group)
    # Parent death must not leave a fetching SDK tree behind after worker SIGKILL.
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise RuntimeError("Could not establish SDK parent-death cleanup")
    if os.getppid() != parent:
        stop_group()

    def parent_watch():
        while True:
            if os.getppid() != parent:
                stop_group()
            time.sleep(0.25)

    threading.Thread(target=parent_watch, daemon=True).start()
    sys.argv = [module, output]
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
