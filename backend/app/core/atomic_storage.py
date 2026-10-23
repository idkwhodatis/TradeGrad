"""No-clobber publication on a local filesystem honoring file/directory fsync.

This is an fsync protocol, not power-loss certification. A durability boundary
must already be durably provisioned, and its ancestors must not be concurrently
renamed or removed. Concurrent publishers below that boundary are supported;
hostile writers with access to the storage namespace are not.
"""

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4


class ContentConflict(ValueError):
    """An existing address does not contain the promised immutable bytes."""


def _verify_existing(target: str, sha256: str, size: int, *, dir_fd: int) -> None:
    # O_NOFOLLOW rejects a final symlink; O_NONBLOCK lets fstat reject FIFOs.
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ContentConflict(
                "Existing content-addressed object has invalid size/type"
            )
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ContentConflict("Existing content-addressed object has invalid hash")
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _absolute_path(path: Path) -> Path:
    # Do not normalize away a symlink/.. pair before checking path components.
    if ".." in path.parts:
        raise ContentConflict("Storage paths must not contain parent traversal")
    return path.absolute()


def _open_directory(path: Path) -> int:
    """Pin a directory, refusing symlinks in every path component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _prepare_directory(path: Path, durable_root: Path | None) -> int:
    """Return an open leaf after flushing each entry below a durable boundary.

    With no explicit boundary, the closest existing ancestor is assumed durable.
    This compatibility mode cannot establish durability of ancestors newly made
    by another publisher. Shared stores should always pass their provisioned root.
    """
    path = _absolute_path(path)
    if durable_root is None:
        root = path
        while True:
            try:
                root.lstat()
                break
            except FileNotFoundError:
                root = root.parent
    else:
        root = _absolute_path(durable_root)
    if not path.is_relative_to(root):
        raise ContentConflict("Publication lies outside its durable storage root")

    # Only open, never fsync, the global ancestors above the declared boundary.
    fd = _open_directory(root)
    try:
        for part in path.relative_to(root).parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            try:
                # Flush even EEXIST: another publisher may have just mkdir'd it
                # and may crash before flushing this parent. Descend only after
                # the entry is durable, before creating any file in the leaf.
                os.fsync(fd)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def provision_directory(path: Path, *, durable_parent: Path) -> None:
    """Provision a directory under an explicit, existing durable boundary.

    Installation must choose ``durable_parent`` whose own entry/ancestry is
    already durable on the persistent filesystem. No existing ancestor is
    inferred. This also repairs interrupted/concurrent creation below that
    boundary, flushing the leaf before return; it does not certify power loss.
    """
    from backend.app.core.storage_barrier import storage_guard

    with storage_guard(durable_parent):
        fd = _prepare_directory(path, durable_parent)
        os.close(fd)


def sync_tree(root: Path) -> None:
    """Make staged bytes and entries durable before an atomic snapshot rename."""
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ContentConflict("Snapshot staging must not contain symlinks")
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            raise ContentConflict("Snapshot staging must contain only regular files")
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(path)


def _publish_bytes(
    target: Path, content: bytes, *, sha256: str, durable_root: Path | None = None
) -> None:
    """Publish all bytes or none, then fsync the final directory before returning.

    ``durable_root`` must be an existing, durably provisioned directory containing
    target. Every entry below it is flushed parent-to-child before publication,
    including entries created by concurrent publishers. Without it, all existing
    ancestors are assumed durable. Errors must prevent a referring DB commit;
    errors after link may leave a complete final file safe for identical retry.
    An interrupted temporary can never poison the immutable final name.
    """
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ContentConflict("Input does not match its content address")
    target = _absolute_path(target)
    if not target.name:
        raise ContentConflict("Publication requires a file name")
    directory_fd = _prepare_directory(target.parent, durable_root)
    temporary = f".{target.name}.{uuid4().hex}.tmp"
    created = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        try:
            view = memoryview(content)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("Content write made no progress")
                written += count
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # Link is atomic and never replaces a concurrently completed address.
            os.link(
                temporary,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _verify_existing(target.name, sha256, len(content), dir_fd=directory_fd)
    finally:
        try:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def publish_bytes(
    target: Path, content: bytes, *, sha256: str, durable_root: Path | None = None
) -> None:
    """Guard the durable root for the whole atomic publication, including cleanup.

    Legacy no-root publication is an offline helper outside managed storage; it
    must never target an installation being maintained. Managed callers name the
    root explicitly. No filesystem guard substitutes for PG admission.
    """
    from backend.app.core.storage_barrier import storage_guard

    if durable_root is None:
        return _publish_bytes(target, content, sha256=sha256)
    with storage_guard(durable_root):
        return _publish_bytes(target, content, sha256=sha256, durable_root=durable_root)
