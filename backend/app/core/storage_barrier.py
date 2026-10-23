"""POSIX cooperative storage exclusion, independent of database availability.

Lock the provisioned storage directory itself; no lock file can be replaced and
no new storage namespace is introduced. Trusted operators must not rename the
root or its ancestors, use a different mount/inode, or run legacy writers.
"""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def storage_guard(root: Path, *, exclusive=False):
    from backend.app.core.atomic_storage import _absolute_path, _open_directory
    from backend.app.core.maintenance import MaintenanceBusy

    path = _absolute_path(Path(root))
    fd = _open_directory(path)
    try:
        try:
            fcntl.flock(
                fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            raise MaintenanceBusy(
                "Storage is busy with cold maintenance or a writer; retry later"
            ) from None
        # Root identity is checked again by retention's pinned inventory/unlink.
        yield fd
    finally:
        os.close(fd)
