"""Crash-safe, no-clobber publication of immutable content-addressed objects."""

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4


class ContentConflict(ValueError):
    """An existing address does not contain the promised immutable bytes."""


def _verify_existing(target: Path, sha256: str, size: int) -> None:
    # O_NOFOLLOW rejects a final symlink instead of following an untrusted target.
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
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


def publish_bytes(target: Path, content: bytes, *, sha256: str) -> None:
    """Publish all bytes or none. An interrupted temp can never poison the final name."""
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ContentConflict("Input does not match its content address")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
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
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _verify_existing(target, sha256, len(content))
    finally:
        temporary.unlink(missing_ok=True)
        fsync_directory(target.parent)
